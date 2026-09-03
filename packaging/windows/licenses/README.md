# Redistributed runtime license material (ZeAlfie Windows installer)

ZeAlfie's native Windows installer (ZA-WIN-BOOT-02/03B) embeds a private
CPython 3.13.15 runtime — the **python-build-standalone** (astral-sh)
`install_only` tarball of the `20260901` release
(`cpython-3.13.15+20260901-x86_64-pc-windows-msvc-install_only.tar.gz`,
SHA-256 pinned in `../reproducibility.toml`). Redistribution therefore
carries license material, which this directory stages under
`{app}\assets\licenses` inside the installed application.

## Files

- `CPython-PSF-LICENSE.txt` — the CPython **PSF License Agreement**,
  copied VERBATIM from `python/LICENSE.txt` inside the pinned, SHA-256-
  verified archive (digest of the staged copy:
  `76900732e5f075b725f754000ad5cab1b2cf89e355c1570138476956b6413cd3`).
  CPython itself is the runtime ZeAlfie ships; this is the license that
  governs it.

## Notices that travel inside the runtime itself

The extracted runtime tree already carries its own license/notice files
(installed with the runtime under `{app}\python`), so nothing extra needs
staging for:

- **pip** (bundled with the standalone build): its license texts ship in
  `python\Lib\site-packages\pip-*.dist-info\licenses\` (MIT and the
  vendored-dependency licenses: Requests/Apache-2.0, cachecontrol,
  certifi, distlib, distro, idna, msgpack, packaging, platformdirs,
  pygments, pyproject_hooks, resolvelib, rich, tomli, truststore, urllib3
  etc.). pip is installed from the bundled wheel by `ensurepip` when the
  offline appenv is created.
- **`python\LICENSE.txt`** duplicates the staged PSF license inside the
  runtime directory itself.

## python-build-standalone provenance (not a separate license burden)

- Upstream: `https://github.com/astral-sh/python-build-standalone`
  (founded by Gregory Szorc / indygreg; now maintained by astral-sh, the
  `uv`/`ruff` team).
- The artifact is a normal CPython built from the public CPython source
  (MSVC x64, `x86_64-pc-windows-msvc`); it adds **no new executable
  code** beyond CPython itself. The build tooling's own license (MIT,
  python-build-standalone repository) governs the *build scripts*, not
  the redistributed runtime; it is not shipped inside the artifact.
- Release provenance: date-tagged GitHub releases (`20260901`), each
  asset exposing a SHA-256 digest recorded in
  `packaging/windows/reproducibility.toml`; the Setup is compiled only
  after that digest is verified (fail closed) by the CI acquire step.

For full upstream licensing details see:
- CPython license: https://docs.python.org/3/license.html
- python-build-standalone: https://github.com/astral-sh/python-build-standalone
- pip license texts: inside the installed `{app}\python\Lib\site-packages`.
