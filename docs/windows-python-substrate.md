# Windows private Python substrate — architecture review (ZA-WIN-BOOT-03A)

Status: **architecture / investigation / proof plan ONLY.** No substrate
replacement has been implemented. This document is the recommendation and
proof plan for human approval before any implementation (the next mission,
ZA-WIN-BOOT-03B).

> Replaces the substrate conclusion previously recorded in
> `packaging/windows/reproducibility.toml` ("official python.org full
> installer — NOT the embeddable zip"). That conclusion is superseded by a
> real human-witness isolation defect described below.

---

## 1. Root-cause summary — why the python.org installer fails isolation

The official `python-3.13.15-amd64.exe` is a **WiX Burn bundle** wrapping
MSI packages. Its bundle carries a stable **UpgradeCode** and a
**RelatedBundle** relationship, and the underlying MSI declares a
**MajorUpgrade** rule keyed on that code. The installer is therefore a
*shared, per-user-or-machine singleton provider* — it is explicitly
designed so that re-running it "upgrades" the one registered CPython of a
given minor series rather than installing a second, independent copy.

On a host that already has a python.org CPython of the **same minor
version**, Burn:

1. detects the existing install as a **related bundle** (`Detected related
   bundle … operation: MajorUpgrade`, `Detected previous version - planning
   upgrade`);
2. restores persisted bundle state from that existing install (its previous
   `TargetDir`, `PrependPath`, `Include_*` feature flags, etc.);
3. re-targets the MSI to the **existing** install directory
   (`…\Programs\Python\Python313\`) instead of ZeAlfie's requested
   `TargetDir`, and re-applies the restored feature state (e.g.
   `PrependPath=1`, `Include_test=1`, `Include_doc=1`, `Include_tcltk=1`).

Real human-witness evidence (BOOT-03): after the failed/diverged install,
`py -3.13` still resolved to the user's existing `Python313\python.exe`, and
`%LOCALAPPDATA%\Programs\ZeAlfie\python\python.exe` was never created.

**Conclusion:** the python.org executable installer cannot be coerced into
true private side-by-side deployment with `InstallAllUsers=0` / `TargetDir=`
flags, because those flags are honored only on a clean host. It FAILS the
hard isolation gate (G1/G2) and must be replaced.

---

## 2. Candidate comparison matrix

Candidates:

- **A** — official python.org `python-3.13.x-amd64.exe` (Burn bundle MSI).
- **B** — official CPython **embeddable** zip (`python-3.13.x-embed-amd64.zip`).
- **C** — official Python **NuGet** package (`python` on nuget.org).
- **D** — **python-build-standalone** relocatable standalone CPython
  (`cpython-3.13.x+YYYYMMDD-x86_64-pc-windows-msvc-install_only.tar.gz`).

| Criterion | A (EXE) | B (embeddable) | C (NuGet) | D (pbs) |
| --- | --- | --- | --- | --- |
| Genuine side-by-side isolation | ❌ (shared provider) | ✅ | ✅ (files only) | ✅ |
| Same-minor collision safety | ❌ (MajorUpgrade) | ✅ | ✅ | ✅ |
| Registry independence | ❌ (PythonCore/Uninstall) | ✅ | ✅ | ✅ |
| MSI/Burn independence | ❌ | ✅ | ✅ | ✅ |
| PATH independence | ⚠️ (flag-only, leaky) | ✅ | ✅ | ✅ |
| Launcher independence | ⚠️ | ✅ | ✅ | ✅ |
| File-association independence | ⚠️ | ✅ | ✅ | ✅ |
| `python.exe` | ✅ | ✅ | ✅ | ✅ |
| `pythonw.exe` | ✅ | ❌ | ⚠️/❌ | ✅ |
| pip | ✅ | ❌ (bootstrap only) | ⚠️/❌ | ✅ (recent) |
| `venv` / appenv | ✅ | ⚠️ fragile (`._pth`) | ⚠️ | ✅ |
| Offline wheel install | ✅ | ⚠️ | ⚠️ | ✅ |
| PySide6 (win_amd64 wheels) | ✅ | ⚠️ | ⚠️ | ✅ |
| Native-extension compat | ✅ | ⚠️ | ⚠️ | ✅ |
| Managed child-runtime compat | ✅ | ⚠️ | ⚠️ | ✅ |
| Per-user / no admin | ✅ | ✅ | ✅ | ✅ |
| Relocatability | ❌ | ✅ (by design) | ✅ | ✅ (by design) |
| Reproducibility / pinning | ✅ (ftp SHA) | ✅ | ⚠️ | ✅ (SHA via GH) |
| SHA / provenance quality | ✅ | ✅ | ⚠️ | ✅ |
| Upstream maintenance | ✅ (python.org) | ✅ | ⚠️ (stale) | ✅ (astral-sh) |
| Licensing / redistribution | ✅ PSF | ✅ PSF | ⚠️ | ✅ PSF |
| Artifact size | ~29 MB exe | ~11 MB | ~ | ~47 MB tar.gz |
| Migration complexity | (n/a) | high | medium | low |
| Long-term maintenance burden | (rejected) | high | high | low |

Legend: ✅ satisfies, ⚠️ partial/fragile, ❌ fails.

---

## 3. Recommended substrate

**D — `python-build-standalone`**, specifically the Windows x64 `install_only`
tarball, e.g.:

```
cpython-3.13.15+20260901-x86_64-pc-windows-msvc-install_only.tar.gz
url: https://github.com/astral-sh/python-build-standalone/releases/download/20260901/cpython-3.13.15%2B20260901-x86_64-pc-windows-msvc-install_only.tar.gz
size: 47 042 104 bytes
sha256: 9bcc038a0bf180612ed56dec93d4977d035e80b8d9320ef51a38c287baf134b7
```

(verified live against the `20260901` release on 2026-09-03; the exact tag
is re-pinned at implementation time.)

Rationale:

- It is a **full, normal CPython** (python.exe **and** pythonw.exe, full
  stdlib, pip in recent releases, `venv` works), compiled MSVC so it is ABI
  compatible with the `win_amd64` PyPI wheels ZeAlfie already pins (PySide6,
  shiboken6, …).
- It is **relocatable and application-owned**: it ships as a plain
  directory tree with **no installer, no Burn bundle, no MSI, no registry
  provider state, no launcher, no PATH/association mutation**. There is
  therefore **no upgrade-code / RelatedBundle / MajorUpgrade machinery to
  attach to a host Python** — a same-minor host install is simply invisible
  to it (G1/G2 satisfied by construction).
- It is the industry-standard mechanism for this exact use case (used by
  `uv`, `rye`, `hatch` and CI tooling to provision private CPython).
- It is reproducible and cryptographically pinnable (GitHub release assets
  expose a SHA-256 digest; the download URL embeds the release tag).

This is a **drop-in functional replacement** for the current private CPython
while removing the shared-provider semantics that caused the defect.

---

## 4. Reasons for rejecting the alternatives

- **A — python.org EXE:** rejected for the confirmed isolation defect
  (Section 1). No flag combination provably avoids Burn's RelatedBundle
  MajorUpgrade on a same-minor host; changing flags does not change Burn's
  bundle identity/upgrade code.
- **B — embeddable zip:** rejected as primary. It is genuinely private but
  functionally incomplete for ZeAlfie: **no `pythonw.exe`** (would break the
  windowed GUI launcher contract), `._pth` isolated mode complicates/breaks
  `venv` and pip, and pip must be bootstrapped via `get-pip.py`. Making it
  work would require fragile unsupported hacks — an unacceptable long-term
  maintenance burden for no isolation benefit over D.
- **C — NuGet package:** rejected. Historically stale/lagging releases,
  no bundled pip, `pythonw.exe` availability unclear, and weaker provenance
  than a pinned GitHub release. It offers no advantage over D.

---

## 5. Exact expected filesystem layout

```
{app}\  (%LOCALAPPDATA%\Programs\ZeAlfie)
├── python\                       # extracted install_only tarball ("python/" dir)
│   ├── python.exe
│   ├── pythonw.exe
│   ├── python313.dll / python3.dll
│   ├── Lib\                      # full stdlib
│   │   └── site-packages\pip…    # bundled pip (verify presence at impl)
│   └── … (DLLs, include, etc.)
├── appenv\                       # created via {app}\python\python.exe -m venv
│   └── Scripts\python.exe / pythonw.exe / zealfie.exe / zealfie-gui.exe
├── assets\                       # wheelhouse + bootstrap scripts + icon
└── logs\
```

Unchanged: the appenv is still derived **only** from `{app}\python`, the
offline wheelhouse still lives under `{app}\assets\wheelhouse`, and the
shared ZeSoftware runtime stays at `%LOCALAPPDATA%\zealfie\runtime`.

---

## 6. Exact isolation guarantees

After the replacement, ZeAlfie's substrate provides:

- `{app}\python\python.exe` (+ `pythonw.exe`) exists and reports the pinned
  3.13.x, **regardless of any host Python**.
- No Burn/MajorUpgrade/MSI relationship with any host CPython (there is no
  MSI/Burn at all — only extracted files).
- No `PythonCore` registry write, no shared provider ownership, no
  Apps&Features/uninstall entry created by the substrate itself.
- No `py.exe` launcher install, no PATH mutation, no `.py`/`.pyw`
  association change.
- The host's existing Python(s), `py.exe`, PATH, associations and registry
  state are **byte-for-byte unchanged** (proved by baseline delta, §7).

A host that is already a Python developer's machine with several Pythons is
therefore indistinguishable from a clean host from ZeAlfie's perspective.

---

## 7. Hostile same-minor collision witness (mandatory)

A real, non-clean Windows machine is prepared so that a normal registered
CPython of the **same minor** already exists before ZeAlfie installs:

**PRE-INSTALL host (machine-readable baseline):**

- Python 3.13.x installed via normal python.org mechanisms
  (`…\AppData\Local\Programs\Python\Python313\`);
- `py.exe` present and `py -0p` lists it;
- user PATH and machine PATH captured;
- `PythonCore` registry captured (HKCU + HKLM);
- `.py` association captured;
- Python Apps & Features state captured;
- preferably a second distribution (e.g. Store Python 3.11) present.

**THEN** install ZeAlfie silently. **POST-INSTALL assertions (delta):**

1. existing Python version unchanged;
2. existing Python executable path unchanged;
3. `py -0p` registrations unchanged — no new ZeAlfie registration;
4. existing Python installation directory unchanged;
5. user PATH unchanged;
6. machine PATH unchanged;
7. existing `py.exe` unchanged;
8. no new launcher introduced;
9. `.py`/`.pyw` associations unchanged;
10. no provider MajorUpgrade/repair operation touched the host Python
    (Setup log shows **no** "Detected related bundle" / "MajorUpgrade");
11. no new shared/machine `PythonCore` ownership attributable to ZeAlfie;
12. `{app}\python\python.exe` exists;
13. it reports the pinned 3.13.x;
14. `{app}\appenv` exists;
15. appenv provenance: `sys.base_prefix == {app}\python`,
    `sys.prefix == {app}\appenv`, `sys._base_executable == {app}\python\python.exe`;
16. `zealfie.exe --version`/`--help` pass;
17. offscreen GUI smoke passes;
18. offline wheel provenance (`--no-index --find-links`, no PyPI);
19. a managed child venv can be created from the private runtime.

The witness is **baseline → post-install delta** (reuses/extends the
BOOT-02 `side_effect_witness.py`), not a brittle "clean host" assumption.

## 8. Clean-host witness

Same assertions minus the "unchanged host Python" deltas (nothing to
preserve). Runs on `windows-latest` CI as today. This is the regression gate
that must stay green, but it is **not** sufficient alone — §7 is the
decisive gate.

## 9. Offline / appenv / child-venv proof

Unchanged in principle: the extracted `{app}\python` is used to create
`{app}\appenv`, then `pip install --no-index --find-links
{app}\assets\wheelhouse <zealfie wheel>` (offline). Additionally prove from
the installed appenv: pip works offline; a `venv.create(path,
with_pip=True)` child runtime derives `base_prefix == {app}\python` (the
same runtime-child mechanism already proven in ZA-WIN-BOOT-01/02).

---

## 10. Uninstall ownership recommendation

**Policy: `{app}\python` is removed WITH the installer (option A).**

Justification: unlike the python.org provider (which created independent
Apps&Features/uninstall bookkeeping worth preserving), a `python-build-standalone`
substrate is **fully ZeAlfie-owned files** with no external registration.
Removing it on uninstall is therefore safe and gives correct disk hygiene
(no orphaned 47 MB runtime) without touching any host Python. This also
simplifies the uninstall witness (no "preserve nested CPython" special-case
remains — the earlier Package Cache / PythonCore preservation semantics of
BOOT-02 disappear with the substrate).

Host Python installations remain untouched in all cases. Self-update and
reinstall/repair are unaffected (a fresh Setup re-extracts the pinned
substrate deterministically).

---

## 11. Supply-chain / security assessment

- **Upstream:** `python-build-standalone` — founded by Gregory Szorc
  (indygreg), now maintained under **astral-sh** (the `uv`/`ruff` team). High
  trust; the de-facto standard standalone CPython distribution.
- **Provenance/reproducibility:** date-tagged GitHub releases
  (`20260901`), each asset with a SHA-256 digest; download URL embeds the
  tag (stable, traceable).
- **Licensing:** CPython **PSF** license; redistribution permitted (bundling
  the license text with the artifact is the standard courtesy and should be
  retained).
- **Update process:** a future CPython security fix is propagated by
  re-pinning a new release tag + SHA-256 in `reproducibility.toml` (a
  one-line, fully-reviewed change) and re-running the witness — bounded and
  auditable, rather than relying on an opaque auto-update.
- **No new opaque dependency:** the artifact is a normal CPython built from
  the public CPython source; it does not replace the isolation fix with an
  untrusted binary of unknown provenance.

---

## 12. Exact migration surface (BOOT-03B; NOT implemented here)

**Changes expected:**

- `packaging/windows/reproducibility.toml` — replace the `[cpython]`
  installer URL/`installer_filename`/SHA-256 with the pinned
  `install_only.tar.gz` (URL, filename, sha256, size); note the substrate
  type changed from "python.org installer" to "python-build-standalone".
- Inno payload staging + `packaging/windows/installer/zealfie.iss` — bundle
  the tarball instead of the `.exe`; replace the `[Code]` "run the EXE
  installer" step with "extract the tarball into `{app}\python`" (the
  SHA-256 gate stays, now over the tarball; the CPython-installer
  `0/3010` exit-code handling disappears).
- `packaging/windows/provision.py` / `provision_windows.py` — provision by
  extraction (tar) rather than by executing an installer; drop the
  `python.org installer` argv builder; keep the venv/appenv/offline-install
  primitives.
- `packaging/windows/installer_smoke.py` — provenance assertions unchanged
  (they already assert `base_prefix == {app}\python`); the "private python
  reports pinned version" check stays.
- `packaging/windows/side_effect_witness.py` — **simplify**: the expected
  "per-user PythonCore / Apps&Features entry" checks are removed (a
  `python-build-standalone` substrate creates none); keep the
  PATH/launcher/association baseline-delta pollution checks and add the
  "no new PythonCore/machine registration" delta.
- `.github/workflows/windows-installer-build.yml` — acquire/verify the
  tarball (SHA-256) instead of the EXE; adjust the CPython-provision step;
  keep wheelhouse/compile/install/smoke/uninstall.
- `docs/windows-installer.md` + this doc — record the substrate change.
- `tests/test_windows_installer.py` — update pin-coupling + witness tests.

**Expected to remain UNCHANGED:** appenv architecture; offline ZeAlfie
wheelhouse (wheelhouse.lock.toml + acquire_wheelhouse.py + the 10-wheel
pins); ZeAlfie application source; the self-update pipeline; product
catalog; managed child-runtime design; Inno toolchain pin.

---

## 13. Bounded implementation plan for ZA-WIN-BOOT-03B

1. Pin the exact `python-build-standalone` release tag + `install_only`
   tarball filename + SHA-256 + size in `reproducibility.toml`.
2. Add a deterministic tarball extract step (Python `tarfile`, into
   `{app}\python`) replacing the EXE install, with the same SHA-256
   fail-closed gate.
3. Adjust the `.iss` `[Files]`/`[Code]` (bundle + extract + verify
   `{app}\python\python.exe` + `pythonw.exe` existence).
4. Update `provision`/`provision_windows`, `installer_smoke`, and simplify
   `side_effect_witness.py` per §12.
5. Run the clean-host CI witness (compile → silent install → smoke →
   uninstall) to green.
6. Run the **hostile same-minor collision witness** (§7) on a real
   multi-Python Windows machine — the decisive acceptance gate.
7. Update docs + tests; local commit; HUMAN GATE before push.

---

## 14. Risks / open questions

- **Exact pin:** confirm the final chosen release tag + asset SHA-256 at
  implementation time (the `20260901` example above was verified live but
  the tag must be re-pinned when implementation lands).
- **pip bundling:** verify the chosen `install_only` tarball bundles `pip`
  (recent releases do); if not, bootstrap with `get-pip.py` +
  `--no-index --find-links` from the already-pinned `pip`/`setuptools`/
  `wheel` wheels (bounded, no PyPI).
- **`pythonw.exe`:** verify presence in the chosen tarball (expected in the
  standard Windows build); this is a hard functional requirement for the
  windowed GUI launcher.
- **Extraction layout:** confirm the tarball's top-level `python/` mapping
  to `{app}\python` (adjust the extract step if the layout differs).
- **Relocatability proof:** the clean+hostile witnesses must empirically
  confirm `base_prefix == {app}\python` from an arbitrary `{app}` path.
- **License text bundling:** retain the PSF license notice in the artifact.

---

## 15. Explicit acceptance gates (G1–G10)

- **G1 TRUE ISOLATION** — substrate is application-private; no shared
  provider/upgrade semantics. → met by construction (no installer).
- **G2 SAME-MINOR COLLISION SAFETY** — an existing normal Python 3.13 stays
  untouched. → proved by §7 hostile witness.
- **G3 PRIVATE PROVENANCE** — appenv derives only from `{app}\python`
  (`base_prefix == {app}\python`). → §9.
- **G4 NO HOST POLLUTION** — PATH/launcher/associations/registry unchanged.
  → §7 delta.
- **G5 FULL PYTHON CAPABILITY** — pip, venv, PySide6, ZeAlfie runtime. → §9.
- **G6 OFFLINE INSTALL** — wheelhouse `--no-index --find-links` unchanged. → §9.
- **G7 CHILD RUNTIME COMPAT** — managed child venvs viable. → §9.
- **G8 REPRODUCIBILITY** — pinned artifact + cryptographic verification. →
  pinned tarball SHA-256 (§3, §12).
- **G9 MAINTAINABILITY** — future security updates have a bounded path. →
  re-pin a new tag (§11).
- **G10 SUPPLY-CHAIN ACCEPTABILITY** — trustworthy, redistributable source.
  → astral-sh + PSF (§11).

**Any candidate failing G1 or G2 is rejected regardless of convenience.**
`python-build-standalone` is the only candidate that satisfies G1/G2 while
also meeting G5–G7 (full capability) without fragile hacks.

---

## Human gate

**STOP here.** The substrate has NOT been replaced; no `.iss`, pin, or
implementation has been changed. Await explicit human approval of
`python-build-standalone` before ZA-WIN-BOOT-03B implements it.
