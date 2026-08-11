# Test Running Guide

> Mesures du 2026-08-11 (branche `feature/m1-2d-selective-install`, HEAD `b4b46b2`,
> après hardening T1–T3). Toute évolution de la suite doit mettre ces chiffres à jour.

## Quick Reference

| Tier | Command | Speed | Tests |
|------|---------|-------|-------|
| **FAST** | `.venv/bin/python -m pytest -m "not zealfie_slow and not integration" -q` | ~35–50 s | 991 |
| **TARGETED** (integration only) | `.venv/bin/python -m pytest -m "integration" -q` | ~35 s | 9 |
| **TARGETED** (runtime/release) | `.venv/bin/python -m pytest -m "zealfie_slow" tests/test_runtime_service.py tests/test_runtime_deployment.py tests/test_runtime_hardening.py -q` | ~3–5 min | — |
| **FULL** | `.venv/bin/python -m pytest -q` (basetemp disque, voir plus bas) | ~8–9 min | 1173 |

La commande nue `pytest` (sans argument) fonctionne désormais : `testpaths = tests`
et `norecursedirs` dans `pytest.ini` garantissent que seul `tests/` est collecté.
Les bundles dépaquetés sous `AGENT/review/` ne sont plus collectés.

## Marker Definitions

- **Non marqué (défaut)** → **FAST** : tests unitaires purs, pas de build wheel,
  pas de création de venv, pas de pip, pas de sous-processus runtime.
- **`zealfie_slow`** : tests qui buildent des wheels via `python -m build`, créent
  des venvs, lancent des pip installs, ou font des opérations runtime réelles.
- **`integration`** : cycles end-to-end complets : build, install, launch, upgrade,
  rollback. Les sentinelles les plus chères et les plus importantes.

## Marker Assignment

Markers are assigned at **file level** via `pytestmark = pytest.mark.<marker>` for
files where nearly all tests are `zealfie_slow` or `integration`. Files with a mix
of `zealfie_slow` and fast tests use individual `@pytest.mark.zealfie_slow`
decorations.

### Files marked `zealfie_slow` at module level

| File | Tests | Reason |
|------|-------|--------|
| `test_wheel_building.py` | 28 | Builds real wheels via `python -m build` |
| `test_runtime_service.py` | 63 | Session fixtures build witness wheels; tests use SharedRuntime |
| `test_runtime_deployment.py` | 14 | Builds wheels + creates venvs + pip installs |
| `test_runtime_manager.py` | 14 | Uses SharedRuntime (venv creation) — dominantly slow |

### Files with individual `@pytest.mark.zealfie_slow`

| File | zealfie_slow | Fast | Rationale |
|------|------|------|-----------|
| `test_cli.py` | 6 | 66 | Seuls les cycles witness E2E et les tests runtime créant des venvs sont slow |
| `test_runtime_hardening.py` | 16 | 9 | Tests `witness_wheel` + `rt.create()` slow ; validation `slot_path` et état canonique fast |
| `test_runtime_6b.py` | 6 | 8 | TOCTOU/discard avec witness fixtures slow ; validation slot et JSON canonique fast |
| `test_runtime_6b1.py` | 3 | 8 | Witness fixtures / `rt.create()` slow ; racines JSON non-objet fast |
| `test_runtime_6b2.py` | 1 | 9 | Transaction witness slow ; `save_active_state`/`load_active_state` fast |
| `test_runtime_status.py` | 5 | 5 | `rt.create()` / `venv.create()` slow ; lectures BROKEN/ABSENT fast |
| `test_releases.py` | 2 | 97 | Seuls 2 tests créent de vraies venvs |
| `test_runtime_dependency_deployment.py` | 14 | 2 | Lock mismatch (composant/version/chemin/taille/sha256) : échec avant création de candidat, mais le setup crée une vraie venv → slow |
| `test_product_preparation.py` | 1 | 59 | `test_preparation_does_not_mutate_runtime_or_selection` crée une vraie venv pour vérifier la non-mutation |

Total : **173 tests `zealfie_slow`**, **991 tests FAST**, **9 tests `integration`**,
**1173 tests au total**.

### Files marked `integration` (9 tests)

| File | Tests | Reason |
|------|-------|--------|
| `test_runtime_transaction.py` | 6 | Full upgrade + rollback cycle |
| `test_runtime_witness_cycle.py` | 2 | Full slot lifecycle |
| `test_witness_install_launch.py` | 1 | Build, install, detect, launch |

## Shared Fixtures

Witness component wheels (`witness_component`, `witness_component_v2`,
`witness_second`) are built **once per session** by shared session-scoped fixtures
in `tests/conftest.py`. Tests that need to mutate/copy artifacts must copy into
`tmp_path` first.

CLI test fixtures (`witness_wheel_cli`, `witness_v2_wheel_cli`,
`witness2_wheel_cli`) are aliases to the same shared wheels, defined in
`tests/conftest.py` to avoid duplicate session-scoped builds.

## Tmp Retention Policy (`pytest.ini`)

```ini
tmp_path_retention_count = 1
tmp_path_retention_policy = failed
```

- Les tmp des tests **réussis** sont nettoyés automatiquement.
- Seuls les artefacts du **dernier run échoué** sont conservés (pour diagnostic).
- Un full run réussi ne laisse donc plus ~3,9 Go de basetemp derrière lui.

## FULL runs : scratch disque obligatoire

`/tmp` est une tmpfs de **3,8 Go** : un full run avec basetemp par défaut y fait
ENOSPC (vérifié). Pour tout run lourd (FULL, TARGETED long), utiliser un basetemp
sur le filesystem disque, puis le nettoyer :

```bash
RUN_TMP="$(mktemp -d -p "$HOME/.cache/zealfie-agent-tmp" pytest.XXXXXX)"
TMPDIR="$RUN_TMP" TEMP="$RUN_TMP" TMP="$RUN_TMP" \
  .venv/bin/python -m pytest --basetemp="$RUN_TMP/pytest" -q
rm -rf "$RUN_TMP"
```

Un helper existe : `AGENT/run_pytest_disk_tmp.sh` (auto-nettoyage). Ne jamais
lancer un FULL avec basetemp par défaut dans `/tmp`.

## Tiers de tests : ce qu'il faut conserver (sentinelles)

Objectif : distinguer clairement les tests logiques légers, les vraies
sentinelles d'intégration (à ne **jamais** mocker), et les tests FULL réservés
aux gates milestone/release.

### 1. Tests logiques — peuvent rester légers / être fakes

Ces familles vérifient de la logique pure (contrats internes, validation
canonique, planning) et peuvent utiliser des fakes, fixtures légères ou runtime
factice SANS perdre de garantie produit :

| Famille | Garantie testée |
|---|---|
| `test_dependency_resolver.py` (logique pure) | résolution de dépendances, locks purs |
| `test_runtime_planning.py` | validation de plans, probe dicts |
| `test_runtime_hardening.py` — validation `slot_path`, état canonique | chemins sûrs, états BROKEN/ABSENT |
| `test_runtime_6b*.py` — JSON canonique, racines non-objet | robustesse état sérialisé |
| `test_runtime_status.py` — lectures d'état sans venv | états ABSENT/BROKEN |
| `test_product_*.py` (hors preparation), `test_source_*.py`, `test_cli.py` (formatting), `test_gui.py` | logique catalogue/sélection/acquisition, formatting, GUI offscreen |

Pour ces familles, un futur allègement (fake au lieu de vraie venv) est
**acceptable** tant que la sentinelle réelle correspondante (ci-dessous) reste.

### 2. Vraies sentinelles d'intégration — ne PAS mocker

Ce sont les garanties de non-régression produit. Elles doivent garder une
**infrastructure réelle** (vraie venv, vrai pip offline, vraie bascule,
vrai TOCTOU) :

| Sentinelle | Garantie |
|---|---|
| `tests/integration/test_runtime_transaction.py` (6) | upgrade/rollback/stale complet, atomicité |
| `tests/integration/test_runtime_witness_cycle.py` (2) | cycle de slot complet (create→install→activate) |
| `tests/integration/test_witness_install_launch.py` (1) | build + install + detect + launch réel |
| `tests/test_runtime_deployment.py` (14) | contrat cœur apply : venv candidat, pip, bascule atomique, TOCTOU |
| `tests/test_wheel_building.py` (28) | contrat de build wheel (dont projet racine) |
| `tests/test_cli.py::test_witness_e2e_plan_apply_rollback_via_cli` | sentinelle CLI E2E (7,3 s) |
| TOCTOU/réalité dans `test_runtime_hardening.py` / `test_runtime_6b*.py` | les TOCTOU nécessitent une vraie venv par définition |
| `test_runtime_service.py` (chemins deploy/launch) | orchestration service réelle |

Règle : toute famille allégée en fakes doit garder **au moins une** sentinelle
réelle de sa frontière. Ne jamais remplacer une sentinelle ci-dessus par un fake.

### 3. Tests FULL uniquement — gates milestone/release

- Matrices complètes runtime, recovery, hardening lourd, wheel building, E2E.
- Exécutés **une seule fois par gate** (fermeture de milestone/release), jamais
  après chaque micro-amend.
- Commandes : voir Quick Reference, avec scratch disque obligatoire.

## Coûts mesurés (2026-08-11)

| Métrique | Valeur |
|---|---|
| Tests au total | 1173 |
| FAST (non marqués) | 991 tests, ~35–50 s (cold ~52 s) |
| `zealfie_slow` | 173 tests |
| `integration` | 9 tests, ~35 s |
| FULL | 1173 tests, ~519 s (8 min 39 s) |
| Venvs réels par FULL | 125 (comptés `pyvenv.cfg`) |
| Basetemp par FULL | ~3,9 Go (nettoyé si run réussi) |
| Rétention par défaut pytest | 1 run échoué max (politique `failed`) |
| `/tmp` | tmpfs 3,8 Go — ne jamais y poser un basetemp de FULL |

## Notes

- Test selection uses only standard pytest marker expressions; no external
  pytest plugin options are required.
- `pytest` nu (sans argument) est la commande FULL canonique depuis T1.
- `norecursedirs = .git build dist AGENT .venv` : `AGENT/` contient des bundles
  de review dépaquetés (ex. `AGENT/review/.../tests/`) qui causaient des
  collisions de modules ; ils sont exclus de la collecte.
