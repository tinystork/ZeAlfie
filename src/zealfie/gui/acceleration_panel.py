"""M1-2G — hardware acceleration panel for the product shell.

Displays the acceleration recommendation computed by the service and a
"Configurer le GPU" action for the compatible case.  This widget renders
:class:`~zealfie.host.AccelerationRecommendation` results only — it contains
no subprocess, nvidia-smi, /sys, platform probing, or CUDA compatibility
decision logic.  The configure action routes through
``service.prepare_gpu_setup_intent`` and never claims installation success.
The panel stores the recommendation it is currently displaying and passes
that exact recommendation to ``prepare_gpu_setup_intent`` on click, so the
configure action never triggers a second hardware observation.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from zealfie.host import AccelerationRecommendation, RecommendationStatus


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
        return "GPU acceleration is already ready."
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


class AccelerationPanel(QFrame):
    """A compact hardware acceleration status panel.

    Receives a recommendation via :meth:`set_recommendation`.  The configure
    button only routes through the service's preparatory intent and displays
    an honest message; it never performs or claims an install.
    """

    def __init__(self, service, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._service = service
        self._recommendation: AccelerationRecommendation | None = None
        self._summary_label: QLabel | None = None
        self._detail_label: QLabel | None = None
        self._button: QPushButton | None = None
        self._build_ui()
        self.set_recommendation(None)

    def _build_ui(self) -> None:
        self.setFrameStyle(QFrame.Shape.StyledPanel | QFrame.Shadow.Raised)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

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

        self._detail_label = QLabel()
        self._detail_label.setWordWrap(True)
        self._detail_label.setVisible(False)
        layout.addWidget(self._detail_label)

        self._button = QPushButton("Configurer le GPU")
        self._button.setObjectName("gpuConfigureButton")
        self._button.setMinimumWidth(150)
        self._button.clicked.connect(self._on_configure_clicked)
        self._button.setVisible(False)
        layout.addWidget(self._button)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def set_recommendation(
        self, recommendation: AccelerationRecommendation | None
    ) -> None:
        """Render a recommendation (or an unknown state when ``None``)."""
        self._recommendation = recommendation
        summary = panel_summary(recommendation)
        detail = panel_detail(recommendation)

        if self._summary_label is not None:
            self._summary_label.setText(summary)
        if self._detail_label is not None:
            self._detail_label.setText(detail)
            self._detail_label.setVisible(bool(detail))
        if self._button is not None:
            self._button.setVisible(configure_button_visible(recommendation))

    def set_unknown(self) -> None:
        """Show an honest unknown state with no configure offer."""
        self.set_recommendation(None)

    def set_error(self, message: str) -> None:
        """Show an unknown/error state when the recommendation probe fails."""
        self._recommendation = None
        if self._summary_label is not None:
            self._summary_label.setText("GPU acceleration status is unknown.")
        if self._detail_label is not None:
            self._detail_label.setText(_short(message))
            self._detail_label.setVisible(True)
        if self._button is not None:
            self._button.setVisible(False)

    # ------------------------------------------------------------------
    # Configure action
    # ------------------------------------------------------------------

    def _on_configure_clicked(self) -> None:
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
        self._show_detail(intent.message)

    def _show_detail(self, message: str) -> None:
        if self._detail_label is not None:
            self._detail_label.setText(_short(message))
            self._detail_label.setVisible(True)
