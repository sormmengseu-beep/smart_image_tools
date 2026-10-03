from __future__ import annotations

import io
import json
import secrets

from PIL import Image
from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QColorDialog, QComboBox, QGridLayout, QHBoxLayout, QInputDialog,
    QLabel, QMessageBox, QPushButton, QSpinBox, QStyle, QWidget,
)

from services.converter import (
    BACKGROUND_DISTRIBUTIONS, BACKGROUND_MODES, CANVAS_FITS, GRADIENT_DIRECTIONS,
    ConversionOptions, options_for_image, render_preview,
)


BUILTIN_PRESETS = {
    "White": ("Solid", "#ffffff", "#ffffff", "Vertical"),
    "Black": ("Solid", "#181818", "#181818", "Vertical"),
    "Red": ("Solid", "#e84b4b", "#e84b4b", "Vertical"),
    "Green": ("Solid", "#47b881", "#47b881", "Vertical"),
    "Blue": ("Solid", "#4389e8", "#4389e8", "Vertical"),
    "Pink": ("Solid", "#f3a7c0", "#f3a7c0", "Vertical"),
    "Yellow": ("Solid", "#f4d35e", "#f4d35e", "Vertical"),
    "Sky": ("Linear gradient", "#4389e8", "#d5f3fa", "Vertical"),
    "Sunset": ("Linear gradient", "#f4d35e", "#e85d75", "Diagonal"),
    "Mint": ("Linear gradient", "#d4f5df", "#43aa8b", "Horizontal"),
    "Spotlight": ("Radial gradient", "#ffffff", "#9ca9bd", "Vertical"),
}
CANVAS_PRESETS = {
    "Original size": (0, 0),
    "Custom": None,
    "Square 1080": (1080, 1080),
    "Portrait 1080 x 1350": (1080, 1350),
    "Story 1080 x 1920": (1080, 1920),
    "Landscape 1920 x 1080": (1920, 1080),
}


class BackgroundEditor(QWidget):
    changed = Signal()

    def __init__(self, settings: QSettings) -> None:
        super().__init__()
        self.settings = settings
        self.start_color = "#ffffff"
        self.end_color = "#8bd3dd"
        self.preview_image: Image.Image | None = None
        self.preview_options: ConversionOptions | None = None
        self.preview_key = "preview"
        self.random_seed = secrets.randbits(64)
        self.saved_presets = self._load_presets()
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(12)
        layout.setVerticalSpacing(4)
        self.setMinimumHeight(128)
        self.mode_combo = QComboBox()
        self.mode_combo.addItems([*BACKGROUND_MODES[1:], *BACKGROUND_DISTRIBUTIONS[1:]])
        self.preset_combo = QComboBox()
        self.preset_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.preset_combo.setMinimumContentsLength(12)
        self.save_button = self._icon_button(QStyle.SP_DialogSaveButton, "Save background preset")
        self.delete_button = self._icon_button(QStyle.SP_TrashIcon, "Delete saved preset")
        self.save_button.clicked.connect(self.save_preset)
        self.delete_button.clicked.connect(self.delete_preset)
        colors = QHBoxLayout()
        colors.setContentsMargins(0, 0, 0, 0)
        colors.setSpacing(8)
        self.start_button = QPushButton()
        self.end_button = QPushButton()
        for button in (self.start_button, self.end_button):
            button.setFixedSize(32, 32)
        self.start_button.clicked.connect(lambda: self.choose_color(False))
        self.end_button.clicked.connect(lambda: self.choose_color(True))
        self.swap_button = self._icon_button(QStyle.SP_BrowserReload, "Swap gradient colors")
        self.swap_button.clicked.connect(self.swap_colors)
        self.shuffle_button = self._icon_button(QStyle.SP_BrowserReload, "Generate new random colors")
        self.shuffle_button.clicked.connect(self.shuffle_backgrounds)
        colors.addWidget(self.start_button)
        colors.addWidget(self.end_button)
        colors.addWidget(self.swap_button)
        colors.addWidget(self.shuffle_button)
        colors.addStretch()
        self.direction_combo = QComboBox()
        self.direction_combo.addItems(GRADIENT_DIRECTIONS)
        self.direction_combo.setCurrentText("Vertical")
        self.direction_label = QLabel("Direction")
        self.random_geometry_label = QLabel()
        self.random_geometry_label.setMinimumHeight(36)
        self.canvas_combo = QComboBox()
        self.canvas_combo.addItems(CANVAS_PRESETS)
        self.canvas_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.canvas_combo.setMinimumContentsLength(12)
        self.width_spin = QSpinBox()
        self.height_spin = QSpinBox()
        for spin in (self.width_spin, self.height_spin):
            spin.setRange(1, 20000)
            spin.setValue(1080)
            spin.setSuffix(" px")
            spin.setMinimumHeight(36)
        self.width_spin.setToolTip("Background width")
        self.height_spin.setToolTip("Background height")
        self.fit_combo = QComboBox()
        self.fit_combo.addItems(CANVAS_FITS)
        self.fit_combo.setToolTip("Image fit on the background canvas")
        dimensions = QHBoxLayout()
        dimensions.setContentsMargins(0, 0, 0, 0)
        dimensions.setSpacing(8)
        dimensions.addWidget(self.width_spin, 1)
        dimensions.addWidget(QLabel("x"))
        dimensions.addWidget(self.height_spin, 1)
        dimensions.addWidget(self.fit_combo, 1)
        for combo in (self.mode_combo, self.preset_combo, self.direction_combo, self.canvas_combo, self.fit_combo):
            combo.setMinimumHeight(36)
        self.preview_label = QLabel()
        self.preview_label.setFixedSize(112, 48)
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setToolTip("Background preview")
        layout.addWidget(QLabel("Style"), 0, 0)
        layout.addWidget(self.mode_combo, 0, 1)
        layout.addWidget(QLabel("Preset"), 0, 2)
        layout.addWidget(self.preset_combo, 0, 3)
        layout.addWidget(self.save_button, 0, 4)
        layout.addWidget(self.delete_button, 0, 5)
        layout.addWidget(QLabel("Colors"), 1, 0)
        layout.addLayout(colors, 1, 1)
        layout.addWidget(self.direction_label, 1, 2)
        layout.addWidget(self.direction_combo, 1, 3)
        layout.addWidget(self.random_geometry_label, 1, 3)
        layout.addWidget(self.preview_label, 1, 4, 1, 2)
        layout.addWidget(QLabel("Canvas"), 2, 0)
        layout.addWidget(self.canvas_combo, 2, 1)
        layout.addLayout(dimensions, 2, 2, 1, 4)
        layout.setColumnStretch(1, 1)
        layout.setColumnStretch(3, 1)
        self._refresh_presets()
        self.preset_combo.activated.connect(self.apply_preset)
        self.mode_combo.currentTextChanged.connect(self._edited)
        self.direction_combo.currentTextChanged.connect(self._edited)
        self.canvas_combo.currentTextChanged.connect(self._canvas_selected)
        self.width_spin.valueChanged.connect(self._dimensions_edited)
        self.height_spin.valueChanged.connect(self._dimensions_edited)
        self.fit_combo.currentTextChanged.connect(self._settings_edited)
        self._refresh()

    def _icon_button(self, icon: QStyle.StandardPixmap, tooltip: str) -> QPushButton:
        button = QPushButton(self.style().standardIcon(icon), "")
        button.setFixedSize(32, 32)
        button.setStyleSheet("padding: 0;")
        button.setToolTip(tooltip)
        return button

    def _load_presets(self) -> dict:
        try:
            data = json.loads(str(self.settings.value("background_presets", "{}")))
        except (ValueError, TypeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {name: values for name, values in data.items()
                if name and name not in BUILTIN_PRESETS and name != "Custom"
                and isinstance(values, (list, tuple)) and len(values) == 4
                and all(isinstance(value, str) for value in values)
                and values[0] in (*BACKGROUND_MODES[1:], *BACKGROUND_DISTRIBUTIONS[2:])
                and values[3] in GRADIENT_DIRECTIONS
                and all(QColor(color).isValid() and QColor(color).alpha() == 255
                        for color in values[1:3])}

    def _refresh_presets(self, selected: str = "Custom") -> None:
        self.preset_combo.clear()
        self.preset_combo.addItems(["Custom", *BUILTIN_PRESETS, *self.saved_presets])
        self.preset_combo.setCurrentText(selected)
        self.delete_button.setEnabled(selected in self.saved_presets)

    def options(self) -> ConversionOptions:
        mode = self.mode_combo.currentText()
        random_mode = mode in BACKGROUND_DISTRIBUTIONS[1:]
        custom_size = self.canvas_combo.currentText() != "Original size"
        return ConversionOptions(background=self.start_color, background_end=self.end_color,
                                 background_mode="Solid" if random_mode else mode,
                                 gradient_direction=self.direction_combo.currentText(),
                                 canvas_width=self.width_spin.value() if custom_size else 0,
                                 canvas_height=self.height_spin.value() if custom_size else 0,
                                 canvas_fit=self.fit_combo.currentText(),
                                 background_distribution=mode if random_mode else "Same background",
                                 random_seed=self.random_seed if random_mode else 0)

    def _refresh(self) -> None:
        randomized = self.mode_combo.currentText() in BACKGROUND_DISTRIBUTIONS[1:]
        random_colors = self.mode_combo.currentText() == "Random solid"
        gradient = self.mode_combo.currentText() not in {"Solid", "Random solid"}
        self.start_button.setEnabled(not random_colors)
        self.end_button.setEnabled(gradient and not randomized)
        self.swap_button.setEnabled(gradient and not randomized)
        self.shuffle_button.setEnabled(randomized)
        self.preset_combo.setEnabled(not random_colors)
        self.save_button.setEnabled(not random_colors)
        self.delete_button.setEnabled(not random_colors and self.preset_combo.currentText() in self.saved_presets)
        linear = self.mode_combo.currentText() in {"Linear gradient", "Random linear gradient"}
        self.direction_combo.setEnabled(linear)
        self.direction_combo.setVisible(not randomized or linear)
        self.random_geometry_label.setVisible(randomized and not linear)
        self.direction_label.setText("Gradient" if randomized and not linear else "Direction")
        custom_size = self.canvas_combo.currentText() != "Original size"
        for widget in (self.width_spin, self.height_spin, self.fit_combo):
            widget.setEnabled(custom_size)
        options = self.preview_options or options_for_image(self.options(), self.preview_key)
        if options.background_mode == "Radial gradient":
            x, y = options.gradient_center
            self.random_geometry_label.setText(f"Center {x:.0%}, {y:.0%}")
        elif options.gradient_angle is not None:
            self.random_geometry_label.setText(f"Angle {options.gradient_angle:.1f} degrees")
        elif options.background_mode == "Linear gradient":
            self.random_geometry_label.setText(options.gradient_direction)
        else:
            self.random_geometry_label.setText("Solid color")
        self.random_geometry_label.setToolTip(options.gradient_description)
        start = options.background if random_colors else self.start_color
        end = options.background_end if randomized else self.end_color
        start_label = ("Shared center color" if self.mode_combo.currentText() == "Random radial gradient" else
                       "Shared start color" if randomized and not random_colors else "Start color")
        end_label = "Random color for this image" if randomized else "End color"
        for button, color, label in ((self.start_button, start, start_label),
                                     (self.end_button, end, end_label)):
            button.setStyleSheet(f"background: {color}; border: 1px solid #a0a7af; border-radius: 3px; padding: 0;")
            button.setToolTip(f"{label}: {color}")
        self.preview_label.setToolTip(f"{options.background_mode}: {options.background} / "
                                      f"{options.background_end}\n{options.gradient_description}")
        self.direction_combo.setToolTip("Gradient direction")
        subject = self.preview_image if self.preview_image is not None else Image.new("RGBA", (112, 48))
        preview = render_preview(subject, options, (112, 48))
        buffer = io.BytesIO()
        preview.save(buffer, format="PNG")
        pixmap = QPixmap()
        pixmap.loadFromData(buffer.getvalue())
        self.preview_label.setPixmap(pixmap)

    def set_preview_image(self, data: bytes = b"", options: ConversionOptions | None = None,
                          image_key: str = "preview") -> None:
        self.preview_options = options
        self.preview_key = image_key
        if data:
            with Image.open(io.BytesIO(data)) as image:
                self.preview_image = image.convert("RGBA")
        else:
            self.preview_image = None
        self._refresh()

    def _edited(self, *_args) -> None:
        self.preset_combo.setCurrentText("Custom")
        self.delete_button.setEnabled(False)
        self._settings_edited()

    def _settings_edited(self, *_args) -> None:
        self.preview_options = None
        self._refresh()
        self.changed.emit()

    def _canvas_selected(self, name: str) -> None:
        size = CANVAS_PRESETS[name]
        if size and size != (0, 0):
            for spin, value in zip((self.width_spin, self.height_spin), size):
                spin.blockSignals(True)
                spin.setValue(value)
                spin.blockSignals(False)
        self.canvas_combo.setToolTip(name)
        self._settings_edited()

    def _dimensions_edited(self, *_args) -> None:
        self.canvas_combo.blockSignals(True)
        self.canvas_combo.setCurrentText("Custom")
        self.canvas_combo.blockSignals(False)
        self._settings_edited()

    def shuffle_backgrounds(self) -> None:
        self.random_seed = secrets.randbits(64)
        self._settings_edited()

    def choose_color(self, end: bool) -> None:
        color = QColorDialog.getColor(QColor(self.end_color if end else self.start_color),
                                      self, "End color" if end else "Background color")
        if color.isValid():
            if end:
                self.end_color = color.name()
            else:
                self.start_color = color.name()
            self._edited()

    def swap_colors(self) -> None:
        self.start_color, self.end_color = self.end_color, self.start_color
        self._edited()

    def apply_preset(self, _index: int = 0) -> None:
        name = self.preset_combo.currentText()
        values = BUILTIN_PRESETS.get(name, self.saved_presets.get(name))
        if values is None:
            self.delete_button.setEnabled(False)
            return
        mode, self.start_color, self.end_color, direction = values
        current_mode = self.mode_combo.currentText()
        if current_mode in BACKGROUND_DISTRIBUTIONS[2:] and mode in BACKGROUND_MODES[1:]:
            mode = current_mode
        self.mode_combo.blockSignals(True)
        self.direction_combo.blockSignals(True)
        self.mode_combo.setCurrentText(mode)
        self.direction_combo.setCurrentText(direction)
        self.mode_combo.blockSignals(False)
        self.direction_combo.blockSignals(False)
        self.delete_button.setEnabled(name in self.saved_presets)
        self._settings_edited()

    def save_preset(self) -> None:
        name, accepted = QInputDialog.getText(self, "Save background preset", "Preset name")
        name = name.strip()
        if not accepted or not name:
            return
        if name in BUILTIN_PRESETS or name == "Custom":
            QMessageBox.warning(self, "Background preset", "Choose a name other than a built-in preset.")
            return
        self.saved_presets[name] = [self.mode_combo.currentText(), self.start_color,
                                   self.end_color, self.direction_combo.currentText()]
        self.settings.setValue("background_presets", json.dumps(self.saved_presets))
        self._refresh_presets(name)

    def delete_preset(self) -> None:
        name = self.preset_combo.currentText()
        if name in self.saved_presets:
            del self.saved_presets[name]
            self.settings.setValue("background_presets", json.dumps(self.saved_presets))
            self._refresh_presets()
