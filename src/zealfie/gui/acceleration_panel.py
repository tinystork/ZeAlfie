"""M1-2G / M1-2I (I3) — hardware acceleration panel for the product shell.

Displays the acceleration recommendation computed by the service and a
"Configurer le GPU" action for the compatible case.  This widget renders
:class:`~zealfie.host.AccelerationRecommendation` results only — it contains
no subprocess, nvidia-smi, /sys, platform probing, or CUDA compatibility
decision logic.  The configure action routes through
``service.prepare_gpu_setup_intent`` and never claims installation success.
The panel stores the recommendation it is currently displaying — plus the
host capabilities observation it was derived from, when supplied — and
passes those exact stored values to ``prepare_gpu_setup_intent`` and the
GPU plan preview on click, so the configure action never triggers a second
hardware observation.

M1-2I (I3) adds the real install path AFTER the preview: when the
configure click builds a ``PLAN_READY`` accelerated deployment plan, an
"Installer" button is offered; clicking it runs
``service.install_accelerated_runtime`` on a worker QThread (never on the
GUI thread), with honest phase labels + canonical percent values, and a
"Cancel" button visible only while the worker runs AND activation has not
been reached (cancelling after the atomic activation point is a no-op, so
the button is hidden — never an ambiguous state).  Fail-closed default:
a non-``PLAN_READY`` plan offers no Installer button at all.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from zealfie.acceleration import (
    AcceleratedDeploymentPhase,
    AcceleratedPlanStatus,
)
from zealfie.gui.accelerated_install_worker import (
    create_accelerated_install_thread,
)
from zealfie.gui.presentation import (
    accelerated_install_view,
    accelerated_phase_label,
    gpu_plan_preview_lines,
)
from zealfie.host import (
    AccelerationRecommendation,
    HostCapabilities,
    RecommendationStatus,
)


def configure_button_visible(recommendation: AccelerationRecommendation | None) -> bool:
    """Return whether the configure button should be offered.

    Only ``OFFER_SETUP`` offers configuration.  BLOCKED / NOT_APPLICABLE /
    UNKNOWN / ALREADY_READY never offer it.
    """
    if recommendation is None:
        return False
    return recommendation.status is RecommendationStatus.OFFER_SETUP


def panel_summary(recommendation: AccelerationRecommendation | None) -> str:
    """Return the user-facing summary for a recommendation."""
    if recommendation is None:
        return "GPU acceleration status is unknown."
    status = recommendation.status
    if status is RecommendationStatus.OFFER_SETUP:
        gpu = _primary_nvidia_gpu(recommendation)
        if gpu is not None and gpu.model:
            return (
                f"NVIDIA GPU detected ({gpu.model}), driver available — "
                "ZeSoftware GPU support: to configure"
            )
        return (
            "NVIDIA GPU detected, driver available — ZeSoftware GPU support: "
            "to configure"
        )
    if status is RecommendationStatus.ALREADY_READY:
        # ZA-M1-3A.2: ALREADY_READY is a SLOT-STATE verdict (active slot
        # carries valid accelerated-metadata and the recorded closure is
        # verified installed) — never the mere presence of a GPU.  The
        # wording distinguishes it explicitly from the OFFER_SETUP case.
        return (
            "GPU acceleration runtime active and validated "
            "(accelerated closure verified in the active runtime slot)."
        )
    if status is RecommendationStatus.BLOCKED:
        return "NVIDIA GPU detected — compatible driver unavailable."
    if status is RecommendationStatus.NOT_APPLICABLE:
        return "No supported GPU detected — running in CPU mode."
    return "GPU acceleration status is unknown."


def panel_detail(recommendation: AccelerationRecommendation | None) -> str:
    """Return a supporting detail line (``""`` when nothing to add)."""
    if recommendation is None:
        return ""
    if recommendation.status in (
        RecommendationStatus.BLOCKED,
        RecommendationStatus.UNKNOWN,
    ):
        return recommendation.reason or ""
    return ""


def _primary_nvidia_gpu(recommendation):
    for gpu in recommendation.gpus:
        if getattr(gpu, "is_nvidia", False):
            return gpu
    return None


def _short(text: str, limit: int = 240) -> str:
    text = " ".join(str(text or "").split())
    if len(text) > limit:
        text = text[: limit - 1] + "\u2026"
    return text


def _short_multiline(text: str, limit: int = 240) -> str:
    """Collapse whitespace per line, preserving line breaks.

    Same discipline as :func:`_short` (collapse, truncate, never leak
    raw tracebacks) but keeps the line structure so multi-line plan
    previews stay readable in the detail label.
    """
    lines: list[str] = []
    total = 0
    for raw_line in str(text or "").split("\n"):
        line = " ".join(raw_line.split())
        if not line:
            continue
        remaining = limit - total - 1
        if remaining <= 0:
            break
        if len(line) > remaining:
            line = line[:remaining] + "\u2026"
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)


class AccelerationPanel(QFrame):
    """A compact hardware acceleration status panel.

    Receives a recommendation (and, optionally, the host capabilities
    observation it was derived from) via :meth:`set_recommendation`.  The
    configure button only routes through the service's preparatory intent
    and the read-only GPU plan preview, reusing the stored observation;
    it never performs or claims an install.

    ZA-M1-2J.1: the composition root's archive fetcher and install work
    root are accepted (optional, ``None`` by default) and forwarded to
    the accelerated install worker so the KEEP base runtime is
    re-acquired at the exact provenance SHA.  With ``None`` the service
    keeps its fail-closed behaviour (no fetcher -> honest PREPARE
    failure, runtime untouched).
    """

    def __init__(
        self,
        service,
        parent: QWidget | None = None,
        *,
        fetcher=None,
        work_root=None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._fetcher = fetcher
        self._work_root = work_root
        self._recommendation: AccelerationRecommendation | None = None
        self._capabilities: HostCapabilities | None = None
        self._summary_label: QLabel | None = None
        self._detail_label: QLabel | None = None
        self._detail_scroll: QScrollArea | None = None
        self._button: QPushButton | None = None
        self._install_button: QPushButton | None = None
        self._cancel_button: QPushButton | None = None
        self._progress_label: QLabel | None = None
        #: The accelerated deployment plan built by the last configure
        #: click (None until re-preview).  Only a PLAN_READY plan offers
        #: the Installer button.
        self._plan = None
        self._install_thread = None
        self._install_worker = None
        self._install_active = False
        self._build_ui()
        self.set_recommendation(None)

    def _build_ui(self) -> None:
        self.setFrameStyle(QFrame.Shape.StyledPanel | QFrame.Shadow.Raised)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)

        title = QLabel("Acc\u00e9l\u00e9ration mat\u00e9rielle")
        title_font = title.font()
        title_font.setBold(True)
        title.setFont(title_font)
        title.setAlignment(Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(title)

        self._summary_label = QLabel()
        self._summary_label.setWordWrap(True)
        layout.addWidget(self._summary_label)

        # M1-4 LOT B: the GPU plan preview is word-wrapped text that can
        # grow arbitrarily long.  Render it inside a bounded, scrollable
        # container so a long preview never expands the whole shell (and
        # never pushes the product cards below the visible area).  The
        # label keeps its word-wrap semantics and the existing 1600-char
        # truncation discipline in :meth:`_show_detail`.
        self._detail_label = QLabel()
        self._detail_label.setWordWrap(True)

        self._detail_scroll = QScrollArea()
        self._detail_scroll.setObjectName("gpuDetailScrollArea")
        self._detail_scroll.setWidgetResizable(True)
        self._detail_scroll.setMaximumHeight(180)
        self._detail_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._detail_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        # The scroll container must never try to expand vertically to
        # fill extra space — only the (bounded) content height.
        self._detail_scroll.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum
        )
        self._detail_scroll.setWidget(self._detail_label)
        self._detail_scroll.setVisible(False)
        layout.addWidget(self._detail_scroll)

        self._button = QPushButton("Configurer le GPU")
        self._button.setObjectName("gpuConfigureButton")
        self._button.setMinimumWidth(150)
        self._button.clicked.connect(self._on_configure_clicked)
        self._button.setVisible(False)
        layout.addWidget(self._button)

        self._install_button = QPushButton("Installer")
        self._install_button.setObjectName("gpuInstallButton")
        self._install_button.setMinimumWidth(150)
        self._install_button.clicked.connect(self._on_install_clicked)
        self._install_button.setVisible(False)
        layout.addWidget(self._install_button)

        self._progress_label = QLabel()
        self._progress_label.setObjectName("gpuInstallProgressLabel")
        self._progress_label.setWordWrap(True)
        self._progress_label.setVisible(False)
        layout.addWidget(self._progress_label)

        self._cancel_button = QPushButton("Cancel")
        self._cancel_button.setObjectName("gpuCancelButton")
        self._cancel_button.setMinimumWidth(150)
        self._cancel_button.clicked.connect(self._on_cancel_clicked)
        self._cancel_button.setVisible(False)
        layout.addWidget(self._cancel_button)

    def minimumSizeHint(self) -> QSize:
        """Report the wrapped-content height so parent layouts never
        squeeze the frame below what its (word-wrapped) content needs.

        The box layout used by the parent distributes heights from
        size hints, not from height-for-width, and a word-wrapped
        QLabel's own size hint assumes the widest unwrapped line.
        At the frame's actual width the text can wrap one or more
        extra lines; totalHeightForWidth accounts for that.
        """
        mh = super().minimumSizeHint()
        layout = self.layout()
        if layout is not None and layout.hasHeightForWidth():
            width = self.width()
            if width > 0:
                mh.setHeight(max(mh.height(), layout.totalHeightForWidth(width)))
        return mh

    def sizeHint(self) -> QSize:
        sh = super().sizeHint()
        layout = self.layout()
        if layout is not None and layout.hasHeightForWidth():
            width = self.width()
            if width > 0:
                sh.setHeight(max(sh.height(), layout.totalHeightForWidth(width)))
        return sh

    def _set_detail_visible(self, visible: bool) -> None:
        """Show/hide the bounded detail container (not just the label)."""
        if self._detail_scroll is not None:
            self._detail_scroll.setVisible(visible)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def set_recommendation(
        self,
        recommendation: AccelerationRecommendation | None,
        capabilities: HostCapabilities | None = None,
    ) -> None:
        """Render a recommendation (or an unknown state when ``None``).

        *capabilities* is the host observation the recommendation was
        derived from.  When both are supplied, the configure action
        reuses the exact stored pair for the GPU plan preview, so it
        never triggers a second hardware observation.
        """
        self._recommendation = recommendation
        self._capabilities = capabilities
        if not self._install_active:
            # A fresh observation invalidates any previously previewed
            # plan: the Installer is only offered after an explicit
            # preview built from the currently displayed pair.
            self._plan = None
        summary = panel_summary(recommendation)
        detail = panel_detail(recommendation)

        if self._summary_label is not None:
            self._summary_label.setText(summary)
        if self._detail_label is not None:
            self._detail_label.setText(detail)
            self._set_detail_visible(bool(detail))
        if self._button is not None:
            self._button.setVisible(configure_button_visible(recommendation))
        self._update_install_button()

    def set_unknown(self) -> None:
        """Show an honest unknown state with no configure offer."""
        self.set_recommendation(None)

    def set_error(self, message: str) -> None:
        """Show an unknown/error state when the recommendation probe fails."""
        self._recommendation = None
        self._capabilities = None
        if self._summary_label is not None:
            self._summary_label.setText("GPU acceleration status is unknown.")
        if self._detail_label is not None:
            self._detail_label.setText(_short(message))
            self._set_detail_visible(True)
        if self._button is not None:
            self._button.setVisible(False)
        if not self._install_active:
            self._plan = None
        self._update_install_button()

    # ------------------------------------------------------------------
    # Configure action
    # ------------------------------------------------------------------

    def _on_configure_clicked(self) -> None:
        if self._install_active:
            # Defensive: buttons are disabled while a worker runs.
            return
        prepare = getattr(self._service, "prepare_gpu_setup_intent", None)
        if not callable(prepare):
            self._show_detail("GPU configuration is not available in this version.")
            return
        recommendation = self._recommendation
        if recommendation is None:
            self._show_detail("GPU acceleration status is unknown.")
            return
        try:
            intent = prepare(recommendation)
        except Exception as exc:
            self._show_detail(
                f"GPU configuration check failed: {_short(str(exc))}"
            )
            return
        # Build the read-only plan ONCE: the same plan feeds the preview
        # lines and the Installer offer (never a second hardware
        # observation, never a duplicate plan build).
        plan, plan_error = self._obtain_plan()
        lines = [intent.message]
        if plan is not None:
            lines.extend(gpu_plan_preview_lines(plan))
        elif plan_error is not None:
            lines.append(plan_error)
        self._plan = plan
        self._update_install_button()
        self._show_detail("\n".join(lines))

    def _obtain_plan(self):
        """Build the read-only accelerated deployment plan (M1-2H/I3).

        Uses the service's ``build_accelerated_deployment_plan`` when
        available, passing the currently displayed recommendation —
        plus the stored capabilities observation when one was supplied
        — so the plan never triggers a second hardware observation.
        When only a recommendation is stored, the builder is called
        recommendation-only (documented fallback: the service then
        re-observes capabilities itself).

        Returns ``(plan, error_line)``: *plan* is ``None`` when the
        service lacks the builder or the build failed; *error_line*
        carries the honest notice in the failure case (``None`` when the
        method is simply absent).  Never raises.
        """
        builder = getattr(
            self._service, "build_accelerated_deployment_plan", None
        )
        if not callable(builder):
            return None, None
        kwargs: dict = {"recommendation": self._recommendation}
        if self._capabilities is not None and self._recommendation is not None:
            kwargs["capabilities"] = self._capabilities
        try:
            return builder(**kwargs), None
        except Exception as exc:
            return None, f"GPU plan preview unavailable: {_short(str(exc))}"

    def _show_detail(self, message: str) -> None:
        if self._detail_label is not None:
            self._detail_label.setText(_short_multiline(message, limit=1600))
            self._set_detail_visible(True)

    # ------------------------------------------------------------------
    # M1-2I (I3): real install — preview → [Installer] → progression → result
    # ------------------------------------------------------------------

    def _update_install_button(self) -> None:
        """Offer the Installer ONLY for a freshly previewed PLAN_READY plan.

        Honest default: fail-closed plans (NO_ACCELERATED_REQUIREMENTS /
        BLOCKED / UNKNOWN) and missing plan builders never offer an
        install — there is nothing honest to install.  The button is
        also hidden while a worker is running.
        """
        if self._install_button is None:
            return
        offer = (
            not self._install_active
            and self._plan is not None
            and getattr(self._plan, "status", None)
            is AcceleratedPlanStatus.PLAN_READY
        )
        self._install_button.setVisible(offer)

    def _on_install_clicked(self) -> None:
        """Start the transactional accelerated install on a worker thread."""
        if self._install_active:
            return
        install = getattr(self._service, "install_accelerated_runtime", None)
        if not callable(install):
            self._show_detail(
                "Accelerated runtime installation is not available in "
                "this version."
            )
            return
        plan = self._plan
        if plan is None or (
            getattr(plan, "status", None) is not AcceleratedPlanStatus.PLAN_READY
        ):
            # Nothing honest to install — never fabricate a run.
            self._update_install_button()
            return

        self._install_active = True
        if self._button is not None:
            self._button.setEnabled(False)
        self._update_install_button()  # hides Installer while running

        thread, worker = create_accelerated_install_thread(
            self._service,
            plan=plan,
            recommendation=self._recommendation,
            capabilities=self._capabilities,
            fetcher=self._fetcher,
            work_root=self._work_root,
            parent=self,
        )
        self._install_thread = thread
        self._install_worker = worker
        worker.progress.connect(self._on_install_progress)
        worker.finished.connect(self._on_install_finished)
        thread.finished.connect(lambda: self._cleanup_install_thread(thread))

        # Honest initial state: Preparation, no percent until the backend
        # reports one.
        label, percent, _done = accelerated_install_view(())
        self._set_progress_text(label, percent)
        if self._cancel_button is not None:
            self._cancel_button.setVisible(True)
        thread.start()

    def _on_cancel_clicked(self) -> None:
        """Request cooperative cancellation of the running worker."""
        worker = self._install_worker
        if worker is not None:
            worker.cancel()

    def _on_install_progress(self, label: str, percent) -> None:
        """Render one honest (label, percent) view from the worker.

        Once the atomic activation point is reached, cancellation can no
        longer interrupt the deployment — the Cancel button is hidden
        (never an ambiguous no-op state).
        """
        self._set_progress_text(label, percent)
        if label == accelerated_phase_label(
            AcceleratedDeploymentPhase.ACTIVATE
        ):
            if self._cancel_button is not None:
                self._cancel_button.setVisible(False)

    def _set_progress_text(self, label: str, percent) -> None:
        if self._progress_label is None:
            return
        text = str(label)
        if isinstance(percent, int):
            text = f"{text} — {percent}%"
        self._progress_label.setText(text)
        self._progress_label.setVisible(True)

    def _on_install_finished(self, result) -> None:
        """Terminal state: honest summary, never a fake 100%, UI re-enabled."""
        if self._cancel_button is not None:
            self._cancel_button.setVisible(False)
        self._install_active = False
        if self._button is not None:
            self._button.setEnabled(True)
        success = bool(getattr(result, "success", False))
        cancelled = bool(getattr(result, "cancelled", False))
        reason = getattr(result, "reason", None)
        if success:
            # The real end: the backend already reported COMPLETED (100%).
            if self._summary_label is not None:
                self._summary_label.setText("Accelerated runtime ready")
            slot = getattr(result, "active_slot_id", None)
            if slot:
                self._show_detail(f"Activated runtime slot: {slot}")
            else:
                self._show_detail("Accelerated runtime is ready.")
            # The plan was consumed by this deployment; no further
            # install offer until an explicit re-preview.
            self._plan = None
            self._update_install_button()
        elif cancelled:
            if self._summary_label is not None:
                self._summary_label.setText(
                    "Accelerated runtime installation cancelled"
                )
            preserved = getattr(result, "old_runtime_preserved", None)
            if reason:
                text = _short(str(reason))
                if preserved:
                    text += " The previous runtime was left untouched."
                self._show_detail(text)
            else:
                self._show_detail(
                    "Cancelled before any change — the previous "
                    "runtime was left untouched."
                )
            self._update_install_button()  # retry stays available
        else:
            if self._summary_label is not None:
                self._summary_label.setText(
                    "Accelerated runtime installation failed"
                )
            self._show_detail(_short(str(reason or "unknown error")))
            self._update_install_button()  # retry stays available

    def _cleanup_install_thread(self, thread) -> None:
        """Release references once the worker thread has fully exited.

        Mirrors the main-window pattern: never calls ``terminate()`` and
        never deletes the worker here (the worker lifecycle is handled
        inside ``create_accelerated_install_thread`` while the thread
        event loop is still alive).
        """
        if thread is not self._install_thread:
            # Stale callback from an old thread — ignore.
            return
        if thread.isRunning():
            thread.wait(5000)
        thread.deleteLater()
        self._install_worker = None
        self._install_thread = None
