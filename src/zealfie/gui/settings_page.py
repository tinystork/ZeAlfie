"""M1-5-A — user-facing Settings page (Language / Hardware / Runtime / GPU).

Presents the technical details removed from the product shell home page:
language preference, a host hardware summary, runtime state, and the full
hardware-acceleration panel (relocated unchanged from the home page).

The page performs NO hardware probing and NO GPU compatibility logic of its
own.  It renders observations already collected by the service (via the main
window's refresh flow) and hosts the existing
:class:`~zealfie.gui.acceleration_panel.AccelerationPanel`, which remains the
only GPU action surface and calls the service's existing methods.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from zealfie.gui.acceleration_panel import AccelerationPanel
from zealfie.host import HostCapabilities
from zealfie.i18n import Language, get_language, translate


_RUNTIME_STATE_KEYS = {
    "ABSENT": "settings.runtime_absent",
    "READY": "settings.runtime_ready",
    "BROKEN": "settings.runtime_broken",
}


class SettingsPage(QWidget):
    """Stacked page hosting Language, Hardware, Runtime, and GPU sections."""

    back_requested = Signal()
    language_selected = Signal(object)  # Language

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
        self._capabilities: HostCapabilities | None = None
        self._shell_state = None
        self._syncing = False
        # Explicit index -> Language mapping (QComboBox.itemData would
        # coerce a StrEnum to a plain str).
        self._combo_languages = (Language.EN, Language.FR)

        self._build_ui(fetcher=fetcher, work_root=work_root)
        self._render_hardware()
        self._render_runtime()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self, *, fetcher, work_root) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 12, 16, 12)
        outer.setSpacing(10)

        # Back row (fixed, outside the scroll area)
        back_row = QHBoxLayout()
        back_row.setSpacing(6)
        self._back_button = QPushButton(translate("settings.back"))
        self._back_button.setObjectName("settingsBackButton")
        self._back_button.clicked.connect(self.back_requested.emit)
        back_row.addWidget(self._back_button)
        back_row.addStretch()
        outer.addLayout(back_row)

        # Scrollable content
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setObjectName("settingsScrollArea")
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(8)
        scroll.setWidget(content)
        outer.addWidget(scroll)

        # -- Language -----------------------------------------------------
        self._language_title = self._make_section_title("settings.language_title")
        content_layout.addWidget(self._language_title)
        self._language_combo = QComboBox()
        self._language_combo.setObjectName("settingsLanguageCombo")
        self._language_combo.addItem("English", Language.EN)
        self._language_combo.addItem("Français", Language.FR)
        self._language_combo.setCurrentIndex(
            0 if get_language() is Language.EN else 1
        )
        self._language_combo.currentIndexChanged.connect(
            self._on_language_changed
        )
        content_layout.addWidget(self._language_combo)

        # -- Hardware -----------------------------------------------------
        self._hardware_title = self._make_section_title("settings.hardware_title")
        content_layout.addWidget(self._hardware_title)
        self._hardware_label = QLabel()
        self._hardware_label.setObjectName("settingsHardwareLabel")
        self._hardware_label.setWordWrap(True)
        content_layout.addWidget(self._hardware_label)

        # -- Runtime ------------------------------------------------------
        self._runtime_title = self._make_section_title("settings.runtime_title")
        content_layout.addWidget(self._runtime_title)
        self._runtime_label = QLabel()
        self._runtime_label.setObjectName("settingsRuntimeLabel")
        self._runtime_label.setWordWrap(True)
        content_layout.addWidget(self._runtime_label)

        # -- GPU acceleration (relocated panel) ---------------------------
        self.acceleration_panel = AccelerationPanel(
            self._service, self, fetcher=fetcher, work_root=work_root
        )
        self.acceleration_panel.setObjectName("settingsAccelerationPanel")
        content_layout.addWidget(self.acceleration_panel)

        content_layout.addStretch()

    def _make_section_title(self, key: str) -> QLabel:
        label = QLabel(translate(key))
        font = label.font()
        font.setBold(True)
        label.setFont(font)
        return label

    # ------------------------------------------------------------------
    # Data feeds (rendered from observations already collected by the shell)
    # ------------------------------------------------------------------

    def set_capabilities(self, capabilities) -> None:
        """Store and render the host capabilities observation (no probing)."""
        self._capabilities = capabilities
        self._render_hardware()

    def set_shell_state(self, shell_state) -> None:
        """Store and render the product-shell state (runtime summary)."""
        self._shell_state = shell_state
        self._render_runtime()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render_hardware(self) -> None:
        if self._hardware_label is None:
            return
        caps = self._capabilities
        if caps is None:
            self._hardware_label.setText(translate("settings.hardware_unknown"))
            return
        lines: list[str] = []
        if getattr(caps, "os_name", None):
            lines.append(translate("settings.hardware_os", os=caps.os_name))
        if getattr(caps, "cpu_arch", None):
            lines.append(translate("settings.hardware_arch", arch=caps.cpu_arch))
        gpus = getattr(caps, "gpus", ()) or ()
        if gpus:
            for gpu in gpus:
                model = getattr(gpu, "model", None) or getattr(gpu, "vendor", "") or "?"
                lines.append(translate("settings.hardware_gpu", gpu=model))
                if getattr(gpu, "driver_version", None):
                    lines.append(
                        translate("settings.hardware_driver", driver=gpu.driver_version)
                    )
        else:
            lines.append(translate("settings.hardware_none"))
        self._hardware_label.setText(
            "\n".join(lines) or translate("settings.hardware_unknown")
        )

    def _render_runtime(self) -> None:
        if self._runtime_label is None:
            return
        shell = self._shell_state
        if shell is None:
            self._runtime_label.setText(translate("settings.runtime_unknown"))
            return
        state_value = getattr(shell, "runtime_state", None)
        state_str = (
            getattr(state_value, "value", state_value)
            if state_value is not None
            else None
        )
        key = _RUNTIME_STATE_KEYS.get(state_str)
        if key is not None:
            state_text = translate(key)
        else:
            state_text = (
                str(state_str)
                if state_str is not None
                else translate("settings.runtime_unknown")
            )
        lines = [translate("settings.runtime_state", state=state_text)]
        root = getattr(shell, "runtime_root", None)
        if root is not None:
            lines.append(translate("settings.runtime_root", root=str(root)))
        self._runtime_label.setText("\n".join(lines))

    # ------------------------------------------------------------------
    # Language + retranslate
    # ------------------------------------------------------------------

    def _on_language_changed(self, index: int) -> None:
        if self._syncing:
            return
        if 0 <= index < len(self._combo_languages):
            self.language_selected.emit(self._combo_languages[index])

    def retranslate(self) -> None:
        """Re-render every translated label from stored state (no re-probe)."""
        self._back_button.setText(translate("settings.back"))
        self._language_title.setText(translate("settings.language_title"))
        self._hardware_title.setText(translate("settings.hardware_title"))
        self._runtime_title.setText(translate("settings.runtime_title"))
        self._syncing = True
        try:
            self._language_combo.setCurrentIndex(
                0 if get_language() is Language.EN else 1
            )
        finally:
            self._syncing = False
        self._render_hardware()
        self._render_runtime()
