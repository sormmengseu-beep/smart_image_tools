from __future__ import annotations

from pathlib import Path
from string import Template

from PySide6.QtCore import QEvent, QSize, QStandardPaths, Qt
from PySide6.QtGui import QColor, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import QScrollArea, QTableWidget, QTableWidgetItem, QWidget


THEMES = {
    "Light": dict(
        canvas="#f1f3f5", surface="#ffffff", field="#ffffff", text="#24292e",
        muted="#667078", disabled="#92999f", border="#d8dde2", hover="#edf1f4",
        selected="#e0f1eb", selected_text="#145842", header="#f7f9fa",
        alternate="#fafbfc", accent="#18765b", accent_hover="#125f48",
        accent_text="#ffffff", success="#18765b", error="#b83952",
        disabled_bg="#eaedf0", scroll="#bec7cc", preview="#e6eaed",
    ),
    "Dark": dict(
        canvas="#191b1e", surface="#222529", field="#2b2f34", text="#edf0f2",
        muted="#aab1b9", disabled="#767e88", border="#3b4148", hover="#353a41",
        selected="#234b40", selected_text="#dbf7eb", header="#272b30",
        alternate="#25292e", accent="#66d5ac", accent_hover="#8be4c4",
        accent_text="#102e23", success="#7bdbb6", error="#ff8fa5",
        disabled_bg="#25292d", scroll="#525b65", preview="#33383e",
    ),
}


class GlassWorkspace(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.theme = "Dark"

    def set_theme(self, theme: str) -> None:
        self.theme = theme
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(THEMES[self.theme]["canvas"]))


class SettingsScrollArea(QScrollArea):
    def sizeHint(self) -> QSize:
        return self.widget().sizeHint() if self.widget() else super().sizeHint()

    def eventFilter(self, watched, event) -> bool:
        handled = super().eventFilter(watched, event)
        if watched is self.widget() and event.type() == QEvent.LayoutRequest:
            self.updateGeometry()
        return handled


def theme_palette(theme: str) -> QPalette:
    colors = THEMES[theme]
    palette = QPalette()
    for role, key in (
        (QPalette.Window, "canvas"), (QPalette.WindowText, "text"),
        (QPalette.Base, "field"), (QPalette.AlternateBase, "alternate"),
        (QPalette.Text, "text"), (QPalette.Button, "field"), (QPalette.ButtonText, "text"),
        (QPalette.Highlight, "selected"), (QPalette.HighlightedText, "selected_text"),
        (QPalette.ToolTipBase, "text"), (QPalette.ToolTipText, "surface"),
        (QPalette.PlaceholderText, "muted"), (QPalette.Light, "hover"),
        (QPalette.Mid, "border"), (QPalette.Dark, "border"),
    ):
        palette.setColor(role, QColor(colors[key]))
    for role in (QPalette.Text, QPalette.WindowText, QPalette.ButtonText):
        palette.setColor(QPalette.Disabled, role, QColor(colors["disabled"]))
    return palette


def _control_icons(theme: str) -> dict[str, str]:
    colors = THEMES[theme]
    directory = Path(QStandardPaths.writableLocation(QStandardPaths.CacheLocation)) / "appearance"
    resources = {"down": "arrow-down-16.png", "up": "arrow-up-16.png",
                 "check": "standardbutton-apply-16.png"}
    paths = {}
    for name, resource in resources.items():
        source = f":/qt-project.org/styles/commonstyle/images/{resource}"
        paths[name] = source
        try:
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / f"{theme.lower()}-{name}.png"
            if not target.exists():
                pixmap = QPixmap(source)
                if pixmap.isNull():
                    continue
                painter = QPainter(pixmap)
                painter.setCompositionMode(QPainter.CompositionMode_SourceIn)
                painter.fillRect(pixmap.rect(), QColor(colors["accent_text"] if name == "check" else colors["text"]))
                painter.end()
                if not pixmap.save(str(target), "PNG"):
                    continue
            paths[name] = target.as_posix()
        except OSError:
            pass
    return paths


def theme_stylesheet(theme: str) -> str:
    return _STYLESHEET.substitute(THEMES[theme], **_control_icons(theme))


def style_status(table: QTableWidget, item: QTableWidgetItem, state: str) -> None:
    theme = table.window().property("appearance") or "Dark"
    item.setData(Qt.UserRole, state)
    item.setForeground(QColor(THEMES[theme][state]))


def refresh_status_colors(table: QTableWidget) -> None:
    for row in range(table.rowCount()):
        item = table.item(row, table.columnCount() - 1)
        if item is not None:
            style_status(table, item, item.data(Qt.UserRole) or "muted")


_STYLESHEET = Template("""
    QWidget { background: transparent; color: $text; font-family: 'Segoe UI'; font-size: 13px; }
    QMainWindow { background: $canvas; }
    QLabel#brand { font-size: 16px; font-weight: 600; }
    QLabel#title { font-size: 22px; font-weight: 600; }
    QLabel#counts { color: $muted; font-weight: 600; font-size: 12px; }
    QLabel#errors { color: $error; font-weight: 600; }
    QLabel#status { color: $muted; font-size: 12px; }
    QFrame#appHeader, QFrame#themeControls { background: $surface; }
    QTabWidget::pane { border: 0; border-top: 1px solid $border; }
    QTabBar { background: $surface; }
    QTabBar::tab {
        background: $surface; color: $muted; border: 0;
        border-bottom: 3px solid transparent; padding: 11px 18px;
        min-height: 20px; font-weight: 600;
    }
    QTabBar::tab:selected { color: $text; border-bottom: 3px solid $accent; }
    QTabBar::tab:hover:!selected { background: $hover; }
    QTabBar::tab:disabled { color: $disabled; }
    QLineEdit, QComboBox, QSpinBox {
        background: $field; border: 1px solid $border; border-radius: 5px;
        padding: 6px 8px; selection-background-color: $selected; selection-color: $selected_text;
    }
    QLineEdit:hover, QComboBox:hover, QSpinBox:hover { border-color: $muted; }
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border-color: $accent; }
    QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {
        color: $disabled; background: $disabled_bg;
    }
    QComboBox { padding-right: 24px; }
    QComboBox::drop-down { width: 22px; border: 0; }
    QComboBox::down-arrow { image: url("$down"); width: 12px; height: 12px; }
    QComboBox QAbstractItemView {
        background: $field; color: $text; border: 1px solid $border;
        selection-background-color: $selected; selection-color: $selected_text;
        outline: 0; padding: 4px;
    }
    QSpinBox { padding-right: 20px; }
    QSpinBox::up-button, QSpinBox::down-button { width: 18px; border: 0; }
    QSpinBox::up-arrow { image: url("$up"); width: 10px; height: 10px; }
    QSpinBox::down-arrow { image: url("$down"); width: 10px; height: 10px; }
    QPushButton {
        background: $field; border: 1px solid $border; border-radius: 5px;
        padding: 7px 11px; min-height: 18px; font-weight: 600;
    }
    QPushButton:hover { background: $hover; border-color: $muted; }
    QPushButton:pressed { background: $selected; border-color: $accent; }
    QPushButton:focus { border-color: $accent; }
    QPushButton:disabled { color: $disabled; background: $disabled_bg; }
    QPushButton#primary { color: $accent_text; background: $accent; border-color: $accent; }
    QPushButton#primary:hover { background: $accent_hover; border-color: $accent_hover; }
    QPushButton#primary:pressed { background: $accent; }
    QPushButton#primary:disabled { color: $disabled; background: $disabled_bg; border-color: $border; }
    QCheckBox { spacing: 7px; color: $muted; }
    QCheckBox:disabled { color: $disabled; }
    QCheckBox::indicator {
        width: 14px; height: 14px; border: 1px solid $muted;
        border-radius: 3px; background: $field;
    }
    QCheckBox::indicator:hover { border-color: $accent; }
    QCheckBox::indicator:checked { image: url("$check"); border-color: $accent; background: $accent; }
    QCheckBox::indicator:disabled { border-color: $border; background: $disabled_bg; }
    QTableWidget {
        background: $surface; alternate-background-color: $alternate;
        border: 1px solid $border; border-radius: 5px;
        selection-background-color: $selected; selection-color: $selected_text; outline: 0;
    }
    QTableWidget::item { padding: 6px; border-bottom: 1px solid $border; }
    QTableWidget::item:selected { background: $selected; color: $selected_text; }
    QHeaderView { background: $surface; }
    QHeaderView::section {
        background: $header; color: $muted; padding: 10px 8px; border: 0;
        border-bottom: 1px solid $border; font-size: 12px; font-weight: 600;
    }
    QTableCornerButton::section { background: $header; border: 0; }
    QLabel#backgroundPreview { background: $preview; border: 1px solid $border; border-radius: 4px; }
    QScrollBar:vertical { width: 10px; background: $surface; margin: 3px 0; }
    QScrollBar:horizontal { height: 10px; background: $surface; margin: 0 3px; }
    QScrollBar::handle { background: $scroll; border-radius: 4px; }
    QScrollBar::handle:vertical { min-height: 28px; }
    QScrollBar::handle:horizontal { min-width: 28px; }
    QScrollBar::handle:hover { background: $muted; }
    QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
    QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
    QProgressBar { border: 0; background: $border; border-radius: 2px; }
    QProgressBar::chunk { background: $accent; border-radius: 2px; }
    QDialog, QMessageBox, QMenu { background: $surface; }
    QToolTip { background: $text; color: $surface; border: 1px solid $border; padding: 6px; }
""")
