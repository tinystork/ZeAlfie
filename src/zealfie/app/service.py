"""Application-level deployment orchestration service (M0-9/M1-0A).

M0-9.1: read-only offline deployment planning (plan_offline_deployment).
M0-9.2: apply + rollback orchestration using existing runtime primitives.
M0-9.3: CLI commands delegate to this service.
M1-0A: runtime component launch (prepare_launch_plan, launch_component).
M1-1C: shared runtime dependency resolution wired into plan_offline_deployment.
M1-2B: non-blocking managed product launch (spawn_component).
M1-2A: product catalog and product-shell read model (list_products, collect_product_state, get_product_state).
"""

from __future__ import annotations

import sys
from pathlib import Path

from zealfie.components.model import ComponentDefinition
from zealfie.components.registry import ComponentRegistry, UnknownComponentError, default_registry
from zealfie.dependencies import (
    DependencyResolutionError,
    resolve_runtime_dependencies,
)
from zealfie.launching import (
    EntryPointScriptNotFoundError,
    LaunchPlan,
    LaunchResult,
    SpawnedLaunch,
    execute_launch_plan,
    resolve_script,
    spawn_launch_plan,
)
from zealfie.products.catalog import (
    ProductCatalog,
    ProductDescriptor,
    default_catalog,
)
from zealfie.products.state import (
    ProductShellState,
    ProductState,
    collect_product_state,
    get_product_state,
)
from zealfie.releases.manifest import (
    ReleaseManifestError,
    parse_release_manifest_file,
)
from zealfie.releases.model import HostTarget
from zealfie.releases.resolver import ReleaseResolutionError, resolve_local_release
from zealfie.runtime.deployment import apply_deployment_plan
from zealfie.runtime.manager import SharedRuntime
from zealfie.runtime.model import (
    DeploymentResult,
    RuntimeState,
    RuntimeStatus,
)
from zealfie.runtime.planning import (
    DeploymentPlan,
    DesiredComponent,
    DesiredRuntimeState,
    build_deployment_plan,
)
from zealfie.runtime.probe import probe_runtime_distribution


class OfflineReleaseError(ValueError):
    """Raised when an offline release directory cannot be resolved.

    Wraps all lower-level failures (missing manifests, parse errors,
    artifact resolution failures, extra unknown manifests) into a
    single application-layer error type.
    """


# ---------------------------------------------------------------------------
# M1-0A: Launch errors
# ---------------------------------------------------------------------------


class LaunchPreparationError(RuntimeError):
    """Raised when a launch cannot be prepared.

    Each subclass carries an *exit_code* that the CLI can use when
    surfacing the error to the user.  All messages are human-readable
    and free of Python traceback details.
    """


class ComponentNotInstalledError(LaunchPreparationError):
    """The component's distribution is not installed in the runtime."""


class LaunchContractNotSatisfiedError(LaunchPreparationError):
    """None of the component's declared entry-point contracts were found
    in the installed distribution."""


class LaunchScriptNotFoundError(LaunchPreparationError):
    """The matched entry-point script does not exist inside the runtime."""


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ZeAlfieService:
    """Application-level deployment orchestration for ZeAlfie.

    Provides ``plan_offline_deployment`` for preview/planning,
    ``apply_offline_deployment`` for orchestrated apply,
    ``rollback_runtime`` for rollback, and
    ``prepare_launch_plan`` / ``launch_component`` for runtime launch
    (M1-0A).

    M1-2B adds ``spawn_component`` for non-blocking launch.
    M1-2A adds ``list_products``, ``collect_product_state``, and
    ``get_product_state`` for the product-shell read model.

    Dependencies (registry, runtime, catalog, host) are injectable so
    tests can supply synthetic instances.
    """

    def __init__(
        self,
        *,
        registry: ComponentRegistry | None = None,
        runtime: SharedRuntime | None = None,
        catalog: ProductCatalog | None = None,
        host: HostTarget | None = None,
    ) -> None:
        self._registry = registry or default_registry()
        self._runtime = runtime or SharedRuntime()
        self._catalog = catalog or default_catalog()
        self._host = host or HostTarget.from_current_host()

    # ------------------------------------------------------------------
    # Release directory convention
    # ------------------------------------------------------------------
    #
    # Deterministic, minimal, local convention:
    #
    #   release_dir/
    #     <component_id>.toml      -- one release manifest per component
    #     <wheel_filename>.whl     -- wheel artifacts at top level
    #
    # Rules:
    #
    # 1. For every *component_id* in ``registry.available_ids()``, the
    #    file ``<component_id>.toml`` MUST exist at the top level.
    #
    # 2. Each manifest's declared ``component_id`` MUST match its
    #    filename stem.
    #
    # 3. Wheel artifacts referenced by manifests live at the top
    #    level of the release directory.
    #
    # 4. Any ``.toml`` file whose stem does not match a known
    #    component id → fail closed.
    #
    # 5. No recursive scan, no fallback names, no heuristic discovery.
    #
    # 6. (M1-1C) The release directory serves as the local dependency
    #    wheelhouse.  Dependency ``.whl`` files live in the same
    #    top-level directory as component manifests and wheel artifacts.

    # ------------------------------------------------------------------
    # resolve_offline_release_set
    # ------------------------------------------------------------------

    def resolve_offline_release_set(
        self, release_dir: Path
    ) -> DesiredRuntimeState:
        """Resolve a complete offline release set from *release_dir*.

        Reads every manifest, resolves each to a
        :class:`VerifiedArtifact` via the safe local release resolver,
        and returns a complete :class:`DesiredRuntimeState`.

        This is a read-only operation.  No filesystem mutation.
        """
        # Every known component must have a manifest.
        expected_ids = frozenset(self._registry.available_ids())

        if not expected_ids:
            raise OfflineReleaseError(
                "component registry is empty — nothing to plan"
            )

        rd = release_dir
        if not rd.is_dir():
            raise OfflineReleaseError(
                f"release_dir does not exist or is not a directory: {rd}"
            )

        desired_components: list[DesiredComponent] = []

        for component_id in sorted(expected_ids):
            manifest_path = rd / f"{component_id}.toml"
            if not manifest_path.is_file():
                raise OfflineReleaseError(
                    f"missing release manifest for component {component_id!r}: "
                    f"expected {manifest_path}"
                )

            try:
                manifest = parse_release_manifest_file(manifest_path)
            except ReleaseManifestError as exc:
                raise OfflineReleaseError(
                    f"invalid release manifest at {manifest_path}: {exc}"
                ) from exc

            if manifest.component_id != component_id:
                raise OfflineReleaseError(
                    f"manifest component_id mismatch: {manifest_path} declares "
                    f"{manifest.component_id!r}, expected {component_id!r}"
                )

            try:
                verified = resolve_local_release(
                    manifest,
                    registry=self._registry,
                    artifact_root=rd,
                    host=self._host,
                )
            except ReleaseResolutionError as exc:
                raise OfflineReleaseError(
                    f"cannot resolve release for {component_id!r}: {exc}"
                ) from exc

            desired_components.append(
                DesiredComponent(
                    component_id=verified.component_id,
                    version=verified.version,
                    artifact=verified,
                )
            )

        # ------------------------------------------------------------------
        # Fail closed: reject extra .toml files with unknown stems.
        # ------------------------------------------------------------------
        for entry in sorted(rd.iterdir()):
            if not entry.is_file():
                continue
            if entry.suffix != ".toml":
                continue
            stem = entry.stem
            if stem not in expected_ids:
                raise OfflineReleaseError(
                    f"unknown release manifest: {entry.name} — "
                    f"component {stem!r} is not in the registry"
                )

        return DesiredRuntimeState(components=tuple(desired_components))

    # ------------------------------------------------------------------
    # plan_offline_deployment
    # ------------------------------------------------------------------

    def plan_offline_deployment(
        self, release_dir: Path
    ) -> DeploymentPlan:
        """Plan a full offline deployment from *release_dir*.

        Resolves the complete release set for all registry components,
        builds the desired runtime state, then resolves shared runtime
        dependencies from the local wheelhouse (the release directory)
        and builds a read-only deployment plan from the current runtime
        status.

        This is a read-only operation.  No filesystem mutation.
        """
        desired_state = self.resolve_offline_release_set(release_dir)
        runtime_status = self._runtime.status()

        # M1-1C: Resolve shared runtime dependencies from the local
        # wheelhouse.  The wheelhouse is the release directory itself
        # (manifests + wheels coexist at the top level).
        # Primary wheels come from already-verified component artifacts.
        # Active extras come from the trusted component registry's
        # ``required_extras`` (already canonicalised by the model).
        try:
            primary_wheels: list[tuple[Path, frozenset[str]]] = []
            for dc in desired_state.components:
                definition = self._registry.get(dc.component_id)
                primary_wheels.append(
                    (
                        dc.artifact.path,
                        frozenset(definition.required_extras),
                    )
                )
            if primary_wheels:
                lock = resolve_runtime_dependencies(
                    primary_wheels, wheelhouse=release_dir,
                )
            else:
                lock = None
        except DependencyResolutionError as exc:
            raise OfflineReleaseError(
                f"shared runtime dependency resolution failed: {exc}"
            ) from exc

        return build_deployment_plan(
            desired_state,
            registry=self._registry,
            runtime_status=runtime_status,
            dependency_lock=lock,
        )

    # ------------------------------------------------------------------
    # apply_offline_deployment   (M0-9.2)
    # ------------------------------------------------------------------

    def apply_offline_deployment(
        self, release_dir: Path
    ) -> DeploymentResult:
        """Apply a complete offline deployment from *release_dir*.

        Re-resolves the release set and re-plans fresh at call time
        — a previously generated ``DeploymentPlan`` is never reused
        or persisted.  The resulting plan is applied via the existing
        :func:`apply_deployment_plan` engine.

        This mutates the shared runtime: creates a candidate slot,
        installs all components, validates, and atomically activates.
        """
        plan = self.plan_offline_deployment(release_dir)
        return apply_deployment_plan(
            plan,
            registry=self._registry,
            runtime=self._runtime,
        )

    # ------------------------------------------------------------------
    # rollback_runtime   (M0-9.2)
    # ------------------------------------------------------------------

    def rollback_runtime(self) -> RuntimeStatus:
        """Rollback the shared runtime to the previous active slot.

        Delegates to the injected :class:`SharedRuntime` rollback
        mechanism.  The previous slot becomes active and the current
        active slot becomes the new previous slot.

        This mutates the shared runtime active pointer.
        """
        return self._runtime.rollback()

    # ------------------------------------------------------------------
    # M1-0A: prepare_launch_plan
    # ------------------------------------------------------------------

    def prepare_launch_plan(self, component_id: str) -> LaunchPlan:
        """Prepare a :class:`LaunchPlan` for *component_id* from the
        active shared runtime.

        The component MUST be declared in the trusted registry and its
        distribution MUST be installed in the active runtime slot with a
        satisfied launch contract.  The resolved entry-point script is
        located inside the runtime's scripts directory — never the dev
        venv.

        This is a read-only operation; no process is started.

        Raises
        ------
        UnknownComponentError
            If *component_id* is not in the trusted registry.
        LaunchPreparationError
            If the runtime is ABSENT or BROKEN.
        ComponentNotInstalledError
            If the component's distribution is not installed in the
            runtime.
        LaunchContractNotSatisfiedError
            If the installed distribution does not declare any of the
            expected entry-point contracts.
        LaunchScriptNotFoundError
            If the matched entry-point script cannot be found inside the
            runtime scripts directory.
        """
        # --- 1. Resolve component from trusted registry --------------------
        try:
            definition = self._registry.get(component_id)
        except UnknownComponentError:
            raise  # let CLI surface it directly

        # --- 2. Require READY runtime with an active slot ------------------
        rt_status = self._runtime.status()
        if rt_status.state == RuntimeState.ABSENT:
            raise LaunchPreparationError(
                "shared runtime is absent — create or deploy a runtime first "
                "(zealfie runtime create)"
            )
        if rt_status.state == RuntimeState.BROKEN:
            raise LaunchPreparationError(
                f"shared runtime is broken: {rt_status.reason or 'unknown reason'}"
            )
        if rt_status.active_path is None or rt_status.python_executable is None:
            raise LaunchPreparationError(
                "shared runtime is READY but has no active slot path"
            )

        active_path = rt_status.active_path
        runtime_python = rt_status.python_executable

        # --- 3. Probe the distribution inside the runtime ------------------
        try:
            probe = probe_runtime_distribution(
                runtime_python, definition.distribution_name
            )
        except Exception as exc:
            raise LaunchPreparationError(
                f"could not probe runtime for {definition.distribution_name!r}: {exc}"
            ) from exc

        # --- 3b. Validate probe payload structure (M1-0A C1) ---------------
        _validate_probe_payload(probe, definition.distribution_name)

        # --- 3c. Check installed flag --------------------------------------
        if not probe.get("installed"):
            raise ComponentNotInstalledError(
                f"component {component_id!r} ({definition.distribution_name}) "
                "is not installed in the shared runtime"
            )

        # --- 4. Select a declared entry point (prefer registry order) ------
        entry_point_name = _select_entry_point_name(definition, probe)
        if entry_point_name is None:
            expected = ", ".join(
                f"{c.group}:{c.name}" for c in definition.launch_entry_points
            )
            raise LaunchContractNotSatisfiedError(
                f"component {component_id!r} is installed but none of the "
                f"expected launch entry points ({expected}) were found"
            )

        # --- 5. Resolve the entry-point script in the runtime scripts dir --
        scripts_dir = _runtime_scripts_dir(active_path)
        try:
            script_path = resolve_script(scripts_dir, entry_point_name)
        except EntryPointScriptNotFoundError as exc:
            raise LaunchScriptNotFoundError(
                f"entry-point script {entry_point_name!r} not found in "
                f"the active runtime scripts directory ({scripts_dir})"
            ) from exc

        # --- 6. Build the launch plan --------------------------------------
        return LaunchPlan(
            component_id=component_id,
            executable=script_path,
        )

    # ------------------------------------------------------------------
    # M1-0A: launch_component
    # ------------------------------------------------------------------

    def launch_component(
        self,
        component_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> LaunchResult:
        """Prepare and execute a launch of *component_id* from the active
        shared runtime.

        Calls :meth:`prepare_launch_plan` then
        :func:`~zealfie.launching.execute_launch_plan`.

        Parameters
        ----------
        component_id:
            The trusted component to launch.
        timeout_seconds:
            Seconds to wait for the subprocess.  ``None`` (default) means
            no timeout, which is appropriate for long-running GUI apps.
            Pass an explicit value to impose a deadline.

        Returns
        -------
        LaunchResult
            The captured stdout, stderr, return code, and timeout flag.

        Raises
        ------
        UnknownComponentError
            If *component_id* is not in the trusted registry.
        LaunchPreparationError
            (or subclass) If preparation fails — runtime absent/broken,
            component not installed, contract not satisfied, or script
            not found.
        """
        plan = self.prepare_launch_plan(component_id)
        return execute_launch_plan(plan, timeout_seconds=timeout_seconds)

    # ------------------------------------------------------------------
    # M1-2B: spawn_component (non-blocking launch)
    # ------------------------------------------------------------------

    def spawn_component(
        self,
        component_id: str,
        *,
        env_overrides: dict[str, str] | None = None,
        stdin: int | None = None,
        stdout: int | None = None,
        stderr: int | None = None,
    ) -> "SpawnedLaunch":
        """Prepare and spawn *component_id* as a non-blocking subprocess.

        Calls :meth:`prepare_launch_plan` then
        :func:`~zealfie.launching.spawn_launch_plan`.  Returns
        immediately with a :class:`SpawnedLaunch` handle; does **not**
        wait for the child process to complete.

        The child process inherits the parent environment.  Callers may
        pass *env_overrides* to add extra variables scoped to the child.

        **ZeSolver embedded-host rule:**
        When *component_id* is ``"zesolver"``, the child environment
        automatically receives ``ZESOLVER_EMBEDDED_HOST=1``.  This
        override is scoped to the child ``Popen`` call.  ZeAlfie's own
        process never becomes an embedded host.  The override is applied
        *before* any caller-supplied *env_overrides*, so a caller can
        override it intentionally.

        Parameters
        ----------
        component_id:
            The trusted component to spawn.
        env_overrides:
            Extra environment variables for the child process.
        stdin:
            Child stdin fd.  ``None`` inherits the parent stdin.
        stdout:
            Child stdout fd.  ``None`` inherits the parent stdout.
        stderr:
            Child stderr fd.  ``None`` inherits the parent stderr.

        Returns
        -------
        SpawnedLaunch

        Raises
        ------
        UnknownComponentError
            If *component_id* is not in the trusted registry.
        LaunchPreparationError
            (or subclass) If preparation fails.
        """
        plan = self.prepare_launch_plan(component_id)

        # Build env overrides, starting with the ZeSolver rule.
        merged: dict[str, str] = {}
        if component_id == "zesolver":
            merged["ZESOLVER_EMBEDDED_HOST"] = "1"
        if env_overrides:
            merged.update(env_overrides)

        return spawn_launch_plan(
            plan,
            env_overrides=merged if merged else None,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
        )

    # ------------------------------------------------------------------
    # M1-2A: Product Shell — read model
    # ------------------------------------------------------------------

    @property
    def catalog(self) -> ProductCatalog:
        """The product catalog (all known products).

        Distinct from the component registry which describes "what
        the user chose to manage".
        """
        return self._catalog

    def list_products(self) -> tuple[ProductDescriptor, ...]:
        """Return all known product descriptors from the catalog.

        This is the "what ZeAlfie knows" listing — distinct from
        the component registry which describes "what the user chose
        to manage".
        """
        return self._catalog.list()

    def collect_product_state(
        self,
        *,
        probe_fn: object = None,
    ) -> ProductShellState:
        """Collect product state for every known product against the
        current managed runtime.

        The *managed* set is derived from the current component registry
        (deployment contract).  Products in the catalog but not in the
        registry are ``UNMANAGED``.

        This is a read-only operation.  No mutation, no installation,
        no launch.
        """
        managed_ids = frozenset(self._registry.available_ids())
        return collect_product_state(
            self._catalog,
            self._runtime.status(),
            managed_component_ids=managed_ids,
            probe_fn=probe_fn,
        )

    def get_product_state(
        self,
        product_id: str,
        *,
        probe_fn: object = None,
    ) -> ProductState:
        """Collect state for a single product against the current runtime.

        Raises :class:`~zealfie.products.catalog.UnknownProductError` if
        *product_id* is not in the product catalog.

        This is a read-only operation.
        """
        managed_ids = frozenset(self._registry.available_ids())
        return get_product_state(
            self._catalog,
            product_id,
            self._runtime.status(),
            managed_component_ids=managed_ids,
            probe_fn=probe_fn,
        )

    @property
    def managed_product_ids(self) -> frozenset[str]:
        """Return the set of product ids currently selected for
        management (deployment registry).

        These are products the user has chosen to manage/install;
        they correspond to the ``components.toml`` entries."""
        return frozenset(self._registry.available_ids())


# ---------------------------------------------------------------------------
# M1-0A helpers
# ---------------------------------------------------------------------------

def _validate_probe_payload(
    probe: object,
    distribution_name: str,
) -> None:
    """Validate the structure of a probe payload.

    Raises :class:`LaunchPreparationError` for any malformed field so
    that callers never receive an ``AttributeError`` or ``TypeError``
    from downstream consumers such as :func:`_select_entry_point_name`.

    Rules (aligned with :func:`zealfie.runtime.planning._validate_probe_payload`):

    * ``installed`` must be exactly ``bool``.
    * If ``installed is False``:
        * ``version`` must be present and exactly ``None``.
        * ``entry_points`` must be present, must be a ``list``, and
          must be empty.
    * If ``installed is True``:
        * ``version`` must be a non-empty ``str``.
        * ``entry_points`` must be a ``list``.
        * Every item in ``entry_points`` must be a ``dict`` whose
          ``group`` and ``name`` values are non-empty ``str``.
    """
    if not isinstance(probe, dict):
        raise LaunchPreparationError(
            f"runtime probe for {distribution_name!r} returned an "
            f"unexpected type {type(probe).__name__!r} (expected dict)"
        )

    installed = probe.get("installed")
    if not isinstance(installed, bool):
        raise LaunchPreparationError(
            f"runtime probe for {distribution_name!r} returned "
            f"non-bool 'installed' field: {installed!r}"
        )

    if installed is False:
        # version must exist and be exactly None.
        if "version" not in probe:
            raise LaunchPreparationError(
                f"runtime probe for {distribution_name!r}: "
                "missing 'version' key when installed=False"
            )
        version = probe["version"]
        if version is not None:
            raise LaunchPreparationError(
                f"runtime probe for {distribution_name!r}: "
                f"version must be None when installed=False, "
                f"got {type(version).__name__}={version!r}"
            )
        # entry_points must exist, be a list, and be empty.
        if "entry_points" not in probe:
            raise LaunchPreparationError(
                f"runtime probe for {distribution_name!r}: "
                "missing 'entry_points' key when installed=False"
            )
        entry_points = probe["entry_points"]
        if not isinstance(entry_points, list):
            raise LaunchPreparationError(
                f"runtime probe for {distribution_name!r}: "
                f"entry_points must be a list when installed=False, "
                f"got {type(entry_points).__name__}"
            )
        if len(entry_points) != 0:
            raise LaunchPreparationError(
                f"runtime probe for {distribution_name!r}: "
                f"entry_points must be empty when installed=False, "
                f"got {type(entry_points).__name__} "
                f"with {len(entry_points)} item(s)"
            )
        return

    # installed is True
    version = probe.get("version")
    if not isinstance(version, str) or not version:
        raise LaunchPreparationError(
            f"runtime probe for {distribution_name!r}: "
            f"version must be non-empty str when installed=True, "
            f"got {type(version).__name__}={version!r}"
        )

    entry_points = probe.get("entry_points")
    if not isinstance(entry_points, list):
        raise LaunchPreparationError(
            f"runtime probe for {distribution_name!r} returned "
            f"non-list 'entry_points' field: {entry_points!r}"
        )

    for i, ep in enumerate(entry_points):
        if not isinstance(ep, dict):
            raise LaunchPreparationError(
                f"runtime probe for {distribution_name!r}: "
                f"entry_points[{i}] is not a dict: {ep!r}"
            )
        group = ep.get("group")
        if not isinstance(group, str) or not group:
            raise LaunchPreparationError(
                f"runtime probe for {distribution_name!r}: "
                f"entry_points[{i}] has missing or non-string "
                f"'group': {group!r}"
            )
        name = ep.get("name")
        if not isinstance(name, str) or not name:
            raise LaunchPreparationError(
                f"runtime probe for {distribution_name!r}: "
                f"entry_points[{i}] has missing or non-string "
                f"'name': {name!r}"
            )


def _select_entry_point_name(
    definition: ComponentDefinition,
    probe: dict[str, object],
) -> str | None:
    """Return the script *name* of the first registry entry-point contract
    that is present in the probe's entry points, or ``None``."""
    observed_eps = probe.get("entry_points", [])
    if not isinstance(observed_eps, list):
        return None

    # Build a set of observed (group, name) → name for fast lookup.
    observed: dict[tuple[str, str], str] = {}
    for ep in observed_eps:
        if not isinstance(ep, dict):
            continue  # defense-in-depth: skip non-dict entries
        g = str(ep.get("group", ""))
        n = str(ep.get("name", ""))
        if g and n:
            observed[(g, n)] = n

    # Walk registry contracts in definition order.
    for contract in definition.launch_entry_points:
        key = (contract.group, contract.name)
        if key in observed:
            return observed[key]

    return None


def _runtime_scripts_dir(venv_dir: Path) -> Path:
    """Return the scripts directory inside a runtime venv slot."""
    if sys.platform == "win32":
        return venv_dir / "Scripts"
    else:
        return venv_dir / "bin"
