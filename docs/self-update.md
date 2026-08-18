# ZeAlfie self-update (ZA-M1-4 LOT D)

This document describes how ZeAlfie updates *itself* safely.  The core
invariant is simple and non-negotiable:

> **The running GUI/CLI process NEVER installs into its own environment.**

A process that replaces the very code it is executing is asking for a broken
or half-updated install.  ZeAlfie therefore splits self-update into a
*prepare* phase (performed by the normal process) and an *activate* phase
(performed by a separate, standalone command run when the GUI is not
active).

---

## 1. Architecture overview

```
resolve → acquire → build → verify → stage → persist pending marker
                                                 ↓ (controlled restart handoff)
                              standalone activator: re-verify → pip install
```

1. **Identity** (`selfupdate/identity.py`) — determine *how* ZeAlfie is
   installed.
2. **Resolution** (`selfupdate/resolver.py`) — find the newest release on a
   channel and pin it to an exact commit SHA.
3. **Verification + staging** (`selfupdate/verify.py`) — build a wheel from
   the pinned source and verify it.
4. **Plan** (`selfupdate/plan.py`) — read-only availability check.
5. **Pending marker + activator** (`selfupdate/state.py`,
   `selfupdate/activator.py`) — persist the staged wheel, then a standalone
   command applies it.

The CLI exposes three subcommands:

| Command | Effect | Mutates? |
| --- | --- | --- |
| `zealfie self-update check` | report current/available version | no |
| `zealfie self-update stage` | build + verify + persist the pending marker | writes marker only |
| `zealfie self-update apply` | re-verify + install the staged wheel | yes (the activator) |

`stage` is a distinct subcommand (not a `check --stage` flag) so that
`check` stays strictly read-only — a read-only command must never be able
to mutate state by adding a flag.

---

## 2. Identity / version determination

`detect_identity()` returns a `ZeAlfieIdentity(version, install_mode,
location)`:

* **version** — `importlib.metadata.version("zealfie")`, falling back to
  `"0.0.0"` when the distribution metadata is absent.
* **location** — the resolved package root (`zealfie/__init__.py`'s parent).
* **install_mode** — one of `INSTALLED`, `EDITABLE`, `SOURCE`, `UNKNOWN`:

  1. `SOURCE` — the package root lives inside a tree containing a `.git/`
     directory (a repository checkout).
  2. `EDITABLE` — the distribution carries an editable marker: a
     `direct_url.json` with `dir_info.editable == true`, or a `.pth` file
     whose contents reference an `__editable__` finder.
  3. `INSTALLED` — a normal site-packages install.
  4. `UNKNOWN` — none of the above could be proven.

Detection is deliberately conservative: a mode is never *guessed*.  When the
evidence is ambiguous the result is `UNKNOWN`, and self-update is refused.

---

## 3. Update resolution + immutable provenance

`resolve_available_update(identity, channel, *, resolver, tags_lister)`:

* Lists tags for `tinystork/ZeAlfie` via the GitHub tags endpoint, using the
  same canonical transport posture as every other network path in ZeAlfie
  (`zealfie.net`: proxy-aware TLS-verified opener, bounded transient-only
  retry, reason-code classifier).  The `Authorization` header uses the same
  `GITHUB_TOKEN` / `GH_TOKEN` env pattern and is never leaked into messages.
* Parses tag names as PEP 440 versions (a leading `v`/`V` is accepted);
  non-version tags are ignored.
* Picks the highest version for the **explicit** channel:

  * `stable` — plain `vX.Y.Z` (no pre/dev/post/local component);
  * `beta` — only `vX.Y.Z-beta.N` tags (prereleases of kind `beta`).

  The channel is always explicit (default `stable`); beta is never silently
  selected.
* Compares the chosen version to the current version; if
  `current >= available` the resolution is `up_to_date`.
* Resolves the chosen **tag** to an exact 40-hex **commit SHA** via the
  resolver.  The commit SHA is immutable provenance — a branch name or
  abbreviated SHA is rejected fail-closed.  Only the commit SHA is ever
  passed downstream to acquisition.

---

## 4. Verification

`stage_update(resolution, *, fetcher, work_root)`:

* Acquires the source archive at the exact `resolution.commit_sha` (never a
  mutable ref) via `zealfie.sources.acquisition.acquire_source`.
* Builds a wheel via `zealfie.building.build_wheel`.
* Inspects the wheel (`inspect_wheel`) and fails closed unless the wheel's
  version equals `resolution.available_version` and its distribution name is
  `zealfie`.
* Records the wheel's SHA-256 + size as the integrity proof for later
  re-verification.

Any version / distribution / build / inspection mismatch raises a dedicated
error and **nothing is staged**.  The recorded SHA-256 is never trusted
implicitly — it is always re-verified before install (see §6).

---

## 5. Staged replacement + controlled restart handoff

* `stage_and_persist(...)` runs §4 staging and then atomically writes the
  pending marker (`RuntimeLayout.state_dir / "self-update-pending.json"`,
  schema version 1: `target_version`, `channel`, `commit_sha`,
  `wheel_path`, `wheel_sha256`, `size`, `created_at`).  It **never
  installs**.
* The marker is written with `mkstemp` + `fsync` + `os.replace`, so a reader
  never observes a half-written file, and read leniently (corrupt/absent →
  refuse).

The handoff is *controlled*: the normal GUI/CLI process only ever reaches
`stage`.  The actual replacement happens in a **separate** invocation of
`zealfie self-update apply` when the GUI is not active.

---

## 6. The standalone activator

`apply_pending_update(*, layout, runtime_root)` is the only path that
installs.  It:

1. loads the pending marker leniently (corrupt/absent → refuse);
2. re-verifies the staged wheel **byte-for-byte** against the recorded
   `wheel_sha256` + `size` (never trusts the marker alone);
3. refuses while another ZeAlfie mutation holds the runtime mutation lease
   (`RuntimeMutationLock(...).probe_busy()`);
4. performs the replacement (list-argv subprocess, no shell):

   * **Linux** — in-process:

   ```
   python -m pip install --no-deps --no-index <wheel_path>
   ```

   * **Windows** — external handoff (ZA-M1-4.1): spawns a detached helper
     (`python -m zealfie.selfupdate.windows_helper`) that waits for this
     process to exit, then re-verifies and installs (never installs over the
     running process);

   * **macOS** — `NOT_SUPPORTED_ON_PLATFORM` (documented follow-up).

5. after a successful install, verifies the freshly-installed ZeAlfie version
   equals the staged target (a fresh subprocess), then clears the pending
   marker only on verified success; on failure leaves the marker in place and
   reports the honest error.

A failed `pip install` of a pure-Python wheel leaves the current install
usable — the old version is not removed before the new one is in place, so
the failure mode is "nothing changed", not "broken".

---

## 7. Platform differences

* **Linux** — the activator is implemented in-process (list-argv pip
  subprocess).
* **Windows** — implemented (ZA-M1-4.1): a detached helper performs the
  replacement after the caller exits.  The helper waits for the caller with a
  fail-closed `ctypes` `OpenProcess`/`WaitForSingleObject` wait (a timeout or
  unconfirmable caller leaves the marker in place and exits non-zero), then
  re-verifies the wheel, installs, verifies the installed version equals the
  target, and only then clears the pending marker.  The running process never
  installs over its own environment.
* **macOS** — **not** implemented.  `apply` returns
  `NOT_SUPPORTED_ON_PLATFORM`; it does **not** fake cross-platform readiness.

---

## 8. Rollback

A self-update is a normal `pip install` of a specific wheel version, so
rollback is the ordinary pip operation:

```
python -m pip install zealfie==<old_version>
```

There is no bespoke rollback machinery, and none is needed: the previous
version's wheel remains published on PyPI and can be reinstalled explicitly.

---

## 9. Editable / source behaviour

Self-update is only supported for a normal `INSTALLED` ZeAlfie.  For an
editable install or a source checkout, `build_self_update_plan` returns
`NOT_SUPPORTED` with an honest reason, and the CLI prints it and exits 0
(no-support is not a *failure*).  Users in those modes update the
repository or reinstall:

```
git pull              # source checkout
pip install zealfie   # editable install
```

---

## 10. Versioning policy

* Semantic versioning: `MAJOR.MINOR.PATCH`.
* Keep `0.0.x` for now.
* Git tags: `vX.Y.Z` (stable) and `vX.Y.Z-beta.N` (beta).
* A **release** = a git tag + a built wheel + a recorded SHA-256.
* **No version bump or publication without Tristan's `HUMAN_GATE`.**  The
  current `pyproject.toml` version (`0.0.6`) is unchanged by this lot.

---

## 11. Non-negotiable

* The running process never `pip install`s itself.
* No auto-apply, no silent self-update.
* The running process never `pip install`s itself (Windows uses a detached helper).
* macOS activator is a follow-up, not faked.
* No version bump, no tag, no publish without the human gate.

---

## 12. Witness status

Real Windows HUMAN_GATE (2026-08-19) — **PASS**:

* self-update **0.0.6 → 0.0.7b1** — `stage` then `apply` handoff completed;
  installed version verified equal to target; pending marker cleared on success.
* **corrupted-wheel fail-closed** — a byte-altered staged wheel was refused
  (SHA-256 re-verification), marker preserved, current install untouched.
* **recovery 0.0.7b1 → 0.0.7b2** — a second staged/apply cycle succeeded.

The Windows `ctypes` wait and `msvcrt` mutation-lock backend are additionally
covered hermetically (injected seams) in `tests/test_selfupdate.py`.
