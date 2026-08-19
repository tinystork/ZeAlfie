
# ZeAlfie — Post-ZSSS Product Roadmap

This document records accepted product work intentionally deferred until
after ZeSeestarStacker integration.

No item in this document authorizes implementation by itself.

## Next milestone — Distribution & Maintenance UX

### 1. Runtime storage management

Expose runtime/storage maintenance from Settings.

Goals:

- show ZeAlfie's runtime root;
- show approximate disk usage by:
  - active runtime;
  - previous protected runtime;
  - cached artifacts;
  - obsolete/reclaimable slots;
- provide an explicit "Clean up" / runtime GC action;
- preserve ACTIVE and required PREVIOUS/rollback candidates;
- never infer deletability from age alone;
- reuse the existing runtime/GC authority rather than implementing deletion
  logic in the GUI.

Possible UX:

    Settings
      Storage
        Runtime location
        Space used
        Reclaimable space
        [Clean up…]

A dry-run / preview before destructive cleanup is preferred.

### 2. Native Windows installer

Provide a normal Windows `.exe` installation path.

Goals:

- user does not need Git;
- user does not need to understand virtual environments;
- install ZeAlfie in a location compatible with its existing self-update
  architecture;
- Start Menu / desktop integration as appropriate;
- clean uninstall of ZeAlfie itself;
- user runtime/state must have an explicit retention/removal policy;
- after initial installation, normal upgrades should continue through
  ZeAlfie's transactional self-update mechanism.

Do not replace the proven self-update engine with installer-specific update
logic.

Linux packaging can follow separately.

### 3. French product language review

Perform a user-facing FR/EN terminology audit.

Current subtitle:

    "Lanceur d'astronomie pour moteurs d'imagerie"

is considered inaccurate and must be replaced.

Candidate French product description:

    "Gestionnaire de l'écosystème d'imagerie astronomique"

Candidate French ALFIE backronym:

    "Assistant de Lancement des Flux d'Imagerie Étoilée"

The acronym is secondary to semantic clarity.

Audit at minimum:

- application title/subtitle;
- Settings;
- runtime terminology;
- GPU terminology;
- installation/update wording;
- product cards;
- errors shown to normal users.

### 4. Known follow-up

Investigate install identity detection when a normal packaged virtual
environment happens to live underneath a Git checkout.

A packaged site-packages installation was observed to be classified as
SOURCE because `.git` existed in an ancestor directory.

The current behaviour is fail-closed and therefore safe, but it can disable
self-update unexpectedly.

Fix only with explicit tests preserving SOURCE/editable safety.

## Scheduling

This milestone starts only after ZeSeestarStacker integration is closed and
validated.

Priority after ZSSS:

1. runtime/storage management;
2. Windows installer;
3. language/product terminology;
4. install-identity edge case.

No implementation is authorized by this roadmap entry alone.
