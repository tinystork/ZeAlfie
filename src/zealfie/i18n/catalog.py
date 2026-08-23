"""EN/FR translation catalogues for the ZeAlfie product shell GUI.

The English catalogue (``EN``) is the always-complete source of truth and
the default language.  The French catalogue (``FR``) provides a translation
for every key introduced here.  Keys are stable, dotted identifiers (e.g.
``"app.title"``); lookups go exclusively through :func:`zealfie.i18n.translate`
— widgets never branch on language or index these dicts directly.

Strings here are the *user-visible shell text* only.  Internal log messages,
CLI self-update output, and strings owned by the backend products are
deliberately out of scope.  The CLI self-update (LOT D) stays English;
the GUI self-update banner (ZA-M1-4.2) is translated here under the
``selfupdate.*`` keys.
"""

from __future__ import annotations

EN: dict[str, str] = {
    # -- Application shell (main window) ---------------------------------
    "app.title": "ZeAlfie — Astronomy Launcher For Imaging Engines",
    "app.subtitle": "Astronomy Launcher For Imaging Engines  \U0001f47d",
    "app.known_limitation": (
        "Note: running product installations cannot yet be cancelled."
    ),
    # -- Menu bar / top bar ----------------------------------------------
    "menu.language": "Language",
    "menu.settings": "Settings",
    "menu.open_settings": "Settings…",
    "menu.refresh": "Refresh",
    # -- Status bar / lifecycle wording ----------------------------------
    "status.starting": "Starting\u2026",
    "status.installing": "Installing {name}\u2026",
    "status.updating": "Updating {name}\u2026",
    "status.install_in_progress_wait": (
        "Installation in progress — please wait for it to finish."
    ),
    "status.gpu_install_in_progress_wait": (
        "Accelerated runtime installation in progress — please wait for "
        "it to finish."
    ),
    "status.refresh_deferred": "Installation in progress — refresh deferred",
    "status.refresh_failed": "Refresh failed",
    "status.refresh_failed_after_install": "Refresh failed after installation",
    "status.install_failed": "Installation failed",
    "status.update_failed": "Update failed",
    "status.install_complete_refreshing": (
        "Installation complete — refreshing\u2026"
    ),
    "status.update_complete_refreshing": "Update complete — refreshing\u2026",
    # -- Error messages --------------------------------------------------
    "error.catalog_load": "Could not load product catalog: {exc}",
    "error.collect_state": "Could not collect product state: {exc}",
    "error.install_deps_missing": "install dependencies not configured",
    "error.update_deps_missing": "update dependencies not configured",
    # -- Product card ----------------------------------------------------
    "cards.install": "Install",
    "cards.launch": "Launch",
    "cards.update": "Update",
    "cards.installing": "Installing\u2026",
    "cards.updating": "Updating\u2026",
    "cards.installing_status": "Installing {name}\u2026",
    "cards.updating_status": "Updating {name}\u2026",
    "cards.loading": "Loading\u2026",
    "cards.launching": "Launching {name}\u2026",
    "cards.error_prefix": "Error: {msg}",
    "cards.install_failed": "Install failed: {message}",
    "cards.update_failed": "Update failed: {message}",
    "cards.install_complete_refresh_required": (
        "Installation complete — refresh required"
    ),
    "cards.policy_pin": "Policy: pin ({sha})",
    "cards.channel": "Channel: {channel}",
    # -- Product state labels --------------------------------------------
    "state.runtime_absent": "No runtime — deploy a runtime first",
    "state.runtime_broken": "Runtime broken — check or recreate",
    "state.installed_launchable": "Ready — click Launch to start",
    "state.installed_not_launchable": "Installed but launch contract missing",
    "state.not_installed": "Not installed — click Install to fetch and install",
    "state.probe_failed": "Could not check — probe failed",
    "state.unknown": "Unknown state",
    # -- Action button tooltips ------------------------------------------
    "action.launch_tooltip": "Launch {name}",
    "action.install_tooltip": "Install {name}",
    "action.launch_contract_missing": (
        "Launch contract not satisfied — product is installed but cannot "
        "be launched"
    ),
    # -- Update status ---------------------------------------------------
    "update.checking": "Checking for updates\u2026",
    "update.up_to_date": "Up to date",
    "update.available": "Update available",
    "update.available_sha": "Update available ({sha})",
    "update.check_failed": "Update check failed",
    "update.check_failed_error": "Update check failed: {error}",
    "update.unknown": "Update status unknown",
    # -- Runtime status summary ------------------------------------------
    "runtime.absent": "Runtime: absent",
    "runtime.broken": "Runtime: broken",
    "runtime.ready_none": "Runtime: ready — {total} known, none installed",
    "runtime.ready": "Runtime: ready — {installed}/{total} installed",
    "runtime.managed_suffix": ", {managed} managed",
    # -- Settings page (M1-5-A) ------------------------------------------
    "settings.back": "← Back",
    "settings.language_title": "Language",
    "settings.hardware_title": "Hardware",
    "settings.runtime_title": "Runtime",
    "settings.hardware_os": "Operating system: {os}",
    "settings.hardware_arch": "CPU architecture: {arch}",
    "settings.hardware_gpu": "GPU: {gpu}",
    "settings.hardware_driver": "Driver: {driver}",
    "settings.hardware_none": "No GPU detected",
    "settings.hardware_unknown": "Hardware information unavailable",
    "settings.runtime_state": "State: {state}",
    "settings.runtime_root": "Root: {root}",
    "settings.runtime_unknown": "Runtime status unavailable",
    "settings.runtime_absent": "absent",
    "settings.runtime_ready": "ready",
    "settings.runtime_broken": "broken",
    "settings.runtime_products": "Managed products: {list}",
    "settings.runtime_products_none": "none",
    "settings.runtime_active_slot": "Active slot: {slot}",
    # -- Hardware acceleration panel -------------------------------------
    "gpu.panel_title": "Hardware acceleration",
    "gpu.configure": "Configure GPU",
    "gpu.install": "Install",
    "gpu.cancel": "Cancel",
    "gpu.status_unknown": "GPU acceleration status is unknown.",
    "gpu.offer_setup_nvidia": (
        "NVIDIA GPU detected ({model}), driver available — ZeSoftware GPU "
        "support: to configure"
    ),
    "gpu.offer_setup": (
        "NVIDIA GPU detected, driver available — ZeSoftware GPU support: "
        "to configure"
    ),
    "gpu.already_ready": (
        "GPU acceleration runtime active and validated (accelerated closure "
        "verified in the active runtime slot)."
    ),
    "gpu.blocked": "NVIDIA GPU detected — compatible driver unavailable.",
    "gpu.not_applicable": "No supported GPU detected — running in CPU mode.",
    "gpu.configure_unavailable": (
        "GPU configuration is not available in this version."
    ),
    "gpu.configure_check_failed": "GPU configuration check failed: {error}",
    "gpu.plan_unavailable": "GPU plan preview unavailable: {error}",
    "gpu.install_unavailable": (
        "Accelerated runtime installation is not available in this version."
    ),
    "gpu.ready": "Accelerated runtime ready",
    "gpu.activated_slot": "Activated runtime slot: {slot}",
    "gpu.ready_detail": "Accelerated runtime is ready.",
    "gpu.cancelled": "Accelerated runtime installation cancelled",
    "gpu.cancelled_preserved": "The previous runtime was left untouched.",
    "gpu.cancelled_detail": (
        "Cancelled before any change — the previous runtime was left "
        "untouched."
    ),
    "gpu.failed": "Accelerated runtime installation failed",
    "gpu.unknown_error": "unknown error",
    # -- Compact GPU status badge (M1-5-A) --------------------------------
    "gpu.badge.offer_setup_nvidia": "GPU: {model} — to configure",
    "gpu.badge.offer_setup": "GPU: NVIDIA — to configure",
    "gpu.badge.ready": "GPU: ready",
    "gpu.badge.blocked": "GPU: blocked",
    "gpu.badge.not_applicable": "GPU: CPU mode",
    "gpu.badge.unknown": "GPU: unknown",
    "gpu.badge.installing": "GPU: installing…",
    "gpu.badge.tooltip": "Open Settings",
    # -- GPU onboarding banner (ZA-M1-5-B LOT D) ---------------------------
    "gpu.onboarding.message": (
        "GPU acceleration is available for {product}. Enable it in Settings."
    ),
    "gpu.onboarding.activate": "Enable acceleration",
    "gpu.onboarding.later": "Later",
    # -- Accelerated deployment phase labels -----------------------------
    "phase.preparation": "Preparation",
    "phase.download": "Download",
    "phase.dependency_resolution": "Dependency resolution",
    "phase.runtime_build": "Runtime build",
    "phase.validation": "Validation",
    "phase.activation": "Activation",
    "phase.completed": "Completed",
    "phase.in_progress": "In progress",
    # -- GPU plan preview lines ------------------------------------------
    "plan.no_requirements": "No installed product currently requires GPU acceleration.",
    "plan.cpu_preserved": "The CPU deployment closure is preserved unchanged.",
    "plan.unknown": "GPU acceleration status could not be determined.",
    "plan.no_change": "No accelerated change has been planned.",
    "plan.blocked": "GPU acceleration planning is blocked.",
    "plan.reason": "Reason: {reason}",
    "plan.no_reason": "no reason recorded",
    "plan.hardware": "Hardware: {value}",
    "plan.backend": "Backend: {backend}",
    "plan.products_concerned": "Products concerned: {list}",
    "plan.none": "none",
    "plan.keep": "Keep {product} {version} (commit {commit})",
    "plan.unknown_commit": "unknown",
    "plan.actions": "Planned actions:",
    "plan.action_item": " - {line}",
    "plan.actions_none": "Planned actions: none recorded",
    "plan.host_prereqs": "Host prerequisites:",
    "plan.no_changes_yet": "No changes have been made yet.",
    # -- Self-update banner (ZA-M1-4.2) ---------------------------------
    "selfupdate.ready": "ZeAlfie {version} is ready to be installed.",
    "selfupdate.update_restart": "Update and restart",
    "selfupdate.later": "Later",
    "selfupdate.applying": "Updating\u2026",
    "selfupdate.apply_failed": (
        "The update could not be applied. The current version is still "
        "installed."
    ),
}


FR: dict[str, str] = {
    # -- Application shell (main window) ---------------------------------
    "app.title": "ZeAlfie — Gestionnaire des outils d’imagerie astro",
    "app.subtitle": "Le chef d’orchestre de vos outils d’imagerie astro  \U0001f47d",
    "app.known_limitation": (
        "Remarque : les installations de produits en cours ne peuvent pas "
        "encore être annulées."
    ),
    # -- Menu bar / top bar ----------------------------------------------
    "menu.language": "Langue",
    "menu.settings": "Paramètres",
    "menu.open_settings": "Paramètres…",
    "menu.refresh": "Rafraîchir",
    # -- Status bar / lifecycle wording ----------------------------------
    "status.starting": "Démarrage\u2026",
    "status.installing": "Installation de {name}\u2026",
    "status.updating": "Mise à jour de {name}\u2026",
    "status.install_in_progress_wait": (
        "Installation en cours — veuillez attendre la fin."
    ),
    "status.gpu_install_in_progress_wait": (
        "Installation du runtime accéléré en cours — veuillez attendre "
        "la fin."
    ),
    "status.refresh_deferred": (
        "Installation en cours — rafraîchissement différé"
    ),
    "status.refresh_failed": "Échec du rafraîchissement",
    "status.refresh_failed_after_install": (
        "Échec du rafraîchissement après l'installation"
    ),
    "status.install_failed": "Échec de l'installation",
    "status.update_failed": "Échec de la mise à jour",
    "status.install_complete_refreshing": (
        "Installation terminée — rafraîchissement\u2026"
    ),
    "status.update_complete_refreshing": (
        "Mise à jour terminée — rafraîchissement\u2026"
    ),
    # -- Error messages --------------------------------------------------
    "error.catalog_load": "Impossible de charger le catalogue de produits : {exc}",
    "error.collect_state": "Impossible de collecter l'état des produits : {exc}",
    "error.install_deps_missing": "dépendances d'installation non configurées",
    "error.update_deps_missing": "dépendances de mise à jour non configurées",
    # -- Product card ----------------------------------------------------
    "cards.install": "Installer",
    "cards.launch": "Lancer",
    "cards.update": "Mettre à jour",
    "cards.installing": "Installation\u2026",
    "cards.updating": "Mise à jour\u2026",
    "cards.installing_status": "Installation de {name} en cours\u2026",
    "cards.updating_status": "Mise à jour de {name} en cours\u2026",
    "cards.loading": "Chargement\u2026",
    "cards.launching": "Lancement de {name}\u2026",
    "cards.error_prefix": "Erreur : {msg}",
    "cards.install_failed": "Échec de l'installation : {message}",
    "cards.update_failed": "Échec de la mise à jour : {message}",
    "cards.install_complete_refresh_required": (
        "Installation terminée — rafraîchissement requis"
    ),
    "cards.policy_pin": "Politique : épinglé ({sha})",
    "cards.channel": "Canal : {channel}",
    # -- Product state labels --------------------------------------------
    "state.runtime_absent": "Aucun runtime — déployez d'abord un runtime",
    "state.runtime_broken": "Environnement d'exécution défaillant — vérifiez ou recréez",
    "state.installed_launchable": "Prêt — cliquez sur Lancer pour démarrer",
    "state.installed_not_launchable": "Installé mais contrat de lancement manquant",
    "state.not_installed": (
        "Non installé — cliquez sur Installer pour récupérer et installer"
    ),
    "state.probe_failed": "Vérification impossible",
    "state.unknown": "État inconnu",
    # -- Action button tooltips ------------------------------------------
    "action.launch_tooltip": "Lancer {name}",
    "action.install_tooltip": "Installer {name}",
    "action.launch_contract_missing": (
        "Contrat de lancement non satisfait — le produit est installé mais "
        "ne peut pas être lancé"
    ),
    # -- Update status ---------------------------------------------------
    "update.checking": "Recherche de mises à jour\u2026",
    "update.up_to_date": "À jour",
    "update.available": "Mise à jour disponible",
    "update.available_sha": "Mise à jour disponible ({sha})",
    "update.check_failed": "Échec de la vérification des mises à jour",
    "update.check_failed_error": "Échec de la vérification des mises à jour : {error}",
    "update.unknown": "État de mise à jour inconnu",
    # -- Runtime status summary ------------------------------------------
    "runtime.absent": "Runtime : absent",
    "runtime.broken": "Runtime : défaillant",
    "runtime.ready_none": "Runtime : prêt — {total} connus, aucun installé",
    "runtime.ready": "Runtime : prêt — {installed}/{total} installés",
    "runtime.managed_suffix": ", {managed} gérés",
    # -- Settings page (M1-5-A) ------------------------------------------
    "settings.back": "← Retour",
    "settings.language_title": "Langue",
    "settings.hardware_title": "Matériel",
    "settings.runtime_title": "Runtime",
    "settings.hardware_os": "Système d'exploitation : {os}",
    "settings.hardware_arch": "Architecture CPU : {arch}",
    "settings.hardware_gpu": "GPU : {gpu}",
    "settings.hardware_driver": "Pilote : {driver}",
    "settings.hardware_none": "Aucun GPU détecté",
    "settings.hardware_unknown": "Informations matérielles indisponibles",
    "settings.runtime_state": "État : {state}",
    "settings.runtime_root": "Racine : {root}",
    "settings.runtime_unknown": "État du runtime indisponible",
    "settings.runtime_absent": "absent",
    "settings.runtime_ready": "prêt",
    "settings.runtime_broken": "défaillant",
    "settings.runtime_products": "Produits gérés : {list}",
    "settings.runtime_products_none": "aucun",
    "settings.runtime_active_slot": "Slot actif : {slot}",
    # -- Hardware acceleration panel -------------------------------------
    "gpu.panel_title": "Accélération matérielle",
    "gpu.configure": "Configurer le GPU",
    "gpu.install": "Installer",
    "gpu.cancel": "Annuler",
    "gpu.status_unknown": "État de l'accélération GPU inconnu.",
    "gpu.offer_setup_nvidia": (
        "GPU NVIDIA détecté ({model}), pilote disponible — support GPU "
        "ZeSoftware : à configurer"
    ),
    "gpu.offer_setup": (
        "GPU NVIDIA détecté, pilote disponible — support GPU ZeSoftware : "
        "à configurer"
    ),
    "gpu.already_ready": (
        "Runtime d'accélération GPU actif et validé (environnement GPU "
        "validé dans le slot de runtime actif)."
    ),
    "gpu.blocked": "GPU NVIDIA détecté — pilote compatible indisponible.",
    "gpu.not_applicable": "Aucun GPU pris en charge détecté — exécution en mode CPU.",
    "gpu.configure_unavailable": (
        "La configuration GPU n'est pas disponible dans cette version."
    ),
    "gpu.configure_check_failed": (
        "Échec de la vérification de la configuration GPU : {error}"
    ),
    "gpu.plan_unavailable": "Aperçu du plan GPU indisponible : {error}",
    "gpu.install_unavailable": (
        "L'installation du runtime accéléré n'est pas disponible dans cette "
        "version."
    ),
    "gpu.ready": "Runtime accéléré prêt",
    "gpu.activated_slot": "Slot de runtime activé : {slot}",
    "gpu.ready_detail": "Le runtime accéléré est prêt.",
    "gpu.cancelled": "Installation du runtime accéléré annulée",
    "gpu.cancelled_preserved": "Le runtime précédent a été laissé intact.",
    "gpu.cancelled_detail": (
        "Annulé avant tout changement — le runtime précédent a été laissé "
        "intact."
    ),
    "gpu.failed": "Échec de l'installation du runtime accéléré",
    "gpu.unknown_error": "erreur inconnue",
    # -- Compact GPU status badge (M1-5-A) --------------------------------
    "gpu.badge.offer_setup_nvidia": "GPU : {model} — à configurer",
    "gpu.badge.offer_setup": "GPU : NVIDIA — à configurer",
    "gpu.badge.ready": "GPU : prêt",
    "gpu.badge.blocked": "GPU : bloqué",
    "gpu.badge.not_applicable": "GPU : mode CPU",
    "gpu.badge.unknown": "GPU : inconnu",
    "gpu.badge.installing": "GPU : installation en cours…",
    "gpu.badge.tooltip": "Ouvrir les paramètres",
    # -- GPU onboarding banner (ZA-M1-5-B LOT D) ---------------------------
    "gpu.onboarding.message": (
        "L'accélération GPU est disponible pour {product}. Activez-la dans "
        "les paramètres."
    ),
    "gpu.onboarding.activate": "Activer l'accélération",
    "gpu.onboarding.later": "Plus tard",
    # -- Accelerated deployment phase labels -----------------------------
    "phase.preparation": "Préparation",
    "phase.download": "Téléchargement",
    "phase.dependency_resolution": "Résolution des dépendances",
    "phase.runtime_build": "Construction du runtime",
    "phase.validation": "Validation",
    "phase.activation": "Activation",
    "phase.completed": "Terminé",
    "phase.in_progress": "En cours",
    # -- GPU plan preview lines ------------------------------------------
    "plan.no_requirements": "Aucun produit installé ne nécessite actuellement d'accélération GPU.",
    "plan.cpu_preserved": "L’ensemble des dépendances CPU reste inchangé.",
    "plan.unknown": "L'état de l'accélération GPU n'a pas pu être déterminé.",
    "plan.no_change": "Aucun changement lié à l’accélération n’est prévu.",
    "plan.blocked": "La planification de l'accélération GPU est bloquée.",
    "plan.reason": "Raison : {reason}",
    "plan.no_reason": "aucune raison enregistrée",
    "plan.hardware": "Matériel : {value}",
    "plan.backend": "Backend : {backend}",
    "plan.products_concerned": "Produits concernés : {list}",
    "plan.none": "aucun",
    "plan.keep": "Conserver {product} {version} (commit {commit})",
    "plan.unknown_commit": "inconnu",
    "plan.actions": "Actions planifiées :",
    "plan.action_item": " - {line}",
    "plan.actions_none": "Actions planifiées : aucune enregistrée",
    "plan.host_prereqs": "Prérequis système :",
    "plan.no_changes_yet": "Aucun changement n'a encore été effectué.",
    # -- Self-update banner (ZA-M1-4.2) ---------------------------------
    "selfupdate.ready": "ZeAlfie {version} est prêt à être installé.",
    "selfupdate.update_restart": "Mettre à jour et redémarrer",
    "selfupdate.later": "Plus tard",
    "selfupdate.applying": "Mise à jour\u2026",
    "selfupdate.apply_failed": (
        "La mise à jour n'a pas pu être appliquée. La version actuelle "
        "reste installée."
    ),
    # -- Product descriptions (FR only; EN lives in manifests/products.toml) ---
    "product.description.zesolver": (
        "Résolution astrométrique, rapide ou à l’aveugle — avec analyse du "
        "champ d’étoiles."
    ),
    "product.description.zemosaic": (
        "Préparez et assemblez vos mosaïques du ciel profond — cadrage, "
        "alignement et fusion des panneaux."
    ),
    "product.description.zeseestarstacker": (
        "Le petit stacker pour plein de brutes."
    ),
    "product.description.zeanalyser": (
        "Mesurez ce qu’il y a vraiment dans vos images — FWHM, excentricité, "
        "SNR et statistiques de session."
    ),
}
