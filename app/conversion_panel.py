from __future__ import annotations

from pathlib import Path
from threading import Event
from typing import Callable

from PySide6.QtCore import QSettings, QSize, QThread, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QColorDialog, QComboBox, QFileDialog,
    QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QProgressBar, QPushButton, QSpinBox, QStyle, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from app.workers import TaskWorker
from services.converter import (
    INPUT_FORMATS, OUTPUT_FORMATS, ConversionOptions, ConversionPlan,
    ConversionResult, build_conversion_plan, convert_batch,
)


class ConversionPanel(QWidget):
    busy_changed = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.settings = QSettings("SmartFileRenamer", "SmartFileRenamer")
        self.plan: ConversionPlan | None = None
        self.busy = False
        self.thread: QThread | None = None
        self.worker: TaskWorker | None = None
        self.cancel_event = Event()
        self.background = "#ffffff"
        self._build_ui()

    def _button(self, text: str, icon: QStyle.StandardPixmap, callback: Callable) -> QPushButton:
        button = QPushButton(self.style().standardIcon(icon), text)
        button.clicked.connect(callback)
        return button

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)
        title = QLabel("Batch Image Tools")
        title.setObjectName("title")
        layout.addWidget(title)
        paths = QGridLayout()
        self.folder_input = QLineEdit()
        self.folder_input.setReadOnly(True)
        self.folder_input.setPlaceholderText("Folder containing images")
        self.output_input = QLineEdit()
        self.output_input.setReadOnly(True)
        self.output_input.setPlaceholderText("Output folder")
        self.folder_button = self._button("Select folder", QStyle.SP_DirIcon, self.choose_folder)
        self.output_button = self._button("Output folder", QStyle.SP_DirOpenIcon, self.choose_output)
        for row, (label, field, button) in enumerate((
            ("Source", self.folder_input, self.folder_button),
            ("Output", self.output_input, self.output_button),
        )):
            paths.addWidget(QLabel(label), row, 0)
            paths.addWidget(field, row, 1)
            paths.addWidget(button, row, 2)
        paths.setColumnStretch(1, 1)
        layout.addLayout(paths)

        options = QGridLayout()
        options.setHorizontalSpacing(12)
        options.setVerticalSpacing(10)
        self.operation_combo = QComboBox()
        self.operation_combo.addItems(["Convert images", "Remove background"])
        self.input_combo = QComboBox()
        self.input_combo.addItems(INPUT_FORMATS)
        self.format_combo = QComboBox()
        self.format_combo.addItems(OUTPUT_FORMATS)
        self.quality_spin = QSpinBox()
        self.quality_spin.setRange(1, 100)
        self.quality_spin.setValue(90)
        self.quality_spin.setSuffix(" %")
        self.svg_spin = QSpinBox()
        self.svg_spin.setRange(128, 8192)
        self.svg_spin.setValue(2048)
        self.svg_spin.setSuffix(" px")
        self.background_button = QPushButton()
        self.background_button.setFixedSize(30, 30)
        self.background_button.setToolTip("Background color for JPG and BMP")
        self.background_button.clicked.connect(self.choose_background)
        self._update_background()
        self.recursive_check = QCheckBox("Include subfolders")
        self.first_frame_check = QCheckBox("First frame")
        self.first_frame_check.setToolTip("Convert only the first frame of animated or multipage images")
        options.addWidget(QLabel("Operation"), 0, 0)
        options.addWidget(self.operation_combo, 0, 1)
        options.addWidget(QLabel("Input"), 0, 2)
        options.addWidget(self.input_combo, 0, 3)
        options.addWidget(QLabel("Output"), 0, 4)
        options.addWidget(self.format_combo, 0, 5)
        options.addWidget(QLabel("Quality"), 1, 0)
        options.addWidget(self.quality_spin, 1, 1)
        options.addWidget(QLabel("SVG width"), 1, 2)
        options.addWidget(self.svg_spin, 1, 3)
        options.addWidget(QLabel("Background"), 1, 4)
        options.addWidget(self.background_button, 1, 5)
        checks = QHBoxLayout()
        checks.setSpacing(16)
        checks.addWidget(self.recursive_check)
        checks.addWidget(self.first_frame_check)
        checks.addStretch()
        options.addLayout(checks, 2, 0, 1, 5)
        self.preview_button = self._button("Preview", QStyle.SP_BrowserReload, self.preview)
        options.addWidget(self.preview_button, 2, 5)
        options.setColumnStretch(1, 1)
        options.setColumnStretch(3, 1)
        layout.addLayout(options)

        self.count_label = QLabel("0 images")
        self.count_label.setObjectName("counts")
        layout.addWidget(self.count_label)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Source image", "Dimensions", "Output file", "Status"])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setIconSize(QSize(48, 48))
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(60)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Fixed)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.Interactive)
        self.table.setColumnWidth(1, 110)
        self.table.setColumnWidth(3, 170)
        layout.addWidget(self.table, 1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(4)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.status_label = QLabel("No source folder selected")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        actions = QHBoxLayout()
        self.cancel_button = self._button("Cancel", QStyle.SP_DialogCancelButton, self.cancel_event.set)
        self.cancel_button.setEnabled(False)
        actions.addWidget(self.cancel_button)
        actions.addStretch()
        self.convert_button = self._button("Convert images", QStyle.SP_DialogApplyButton, self.convert)
        self.convert_button.setObjectName("primary")
        self.convert_button.setEnabled(False)
        actions.addWidget(self.convert_button)
        layout.addLayout(actions)
        self.input_widgets = [self.folder_button, self.output_button, self.operation_combo,
                              self.input_combo, self.format_combo, self.quality_spin,
                              self.svg_spin, self.background_button,
                              self.recursive_check, self.first_frame_check, self.preview_button]
        for combo in (self.operation_combo, self.input_combo, self.format_combo):
            combo.currentIndexChanged.connect(self.invalidate)
        for spin in (self.quality_spin, self.svg_spin):
            spin.valueChanged.connect(self.invalidate)
        for check in (self.recursive_check, self.first_frame_check):
            check.toggled.connect(self.invalidate)

    def _update_background(self) -> None:
        self.background_button.setStyleSheet(
            f"background: {self.background}; border: 1px solid #a0a7af; border-radius: 3px;"
        )

    def choose_background(self) -> None:
        color = QColorDialog.getColor(QColor(self.background), self, "Conversion background")
        if color.isValid():
            self.background = color.name()
            self._update_background()
            self.invalidate()

    def choose_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Source images", self.settings.value("conversion_directory", "")
        )
        if chosen:
            self.folder_input.setText(chosen)
            self.output_input.setText(str(Path(chosen) / "converted"))
            self.settings.setValue("conversion_directory", chosen)
            self.invalidate()

    def choose_output(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Conversion output", self.output_input.text())
        if chosen:
            self.output_input.setText(chosen)
            self.invalidate()

    def invalidate(self, *_args) -> None:
        self.plan = None
        self.table.setRowCount(0)
        self.count_label.setText("0 images")
        self.status_label.setText("Preview pending")
        self.convert_button.setEnabled(False)
        self._update_option_states()

    def _update_option_states(self) -> None:
        removing = self.operation_combo.currentText() == "Remove background"
        self.format_combo.setEnabled(not self.busy and not removing)
        self.quality_spin.setEnabled(not self.busy and not removing
                                     and self.format_combo.currentText() in {"JPG", "WebP", "AVIF"})
        self.background_button.setEnabled(not self.busy and not removing
                                          and self.format_combo.currentText() in {"JPG", "BMP"})
        self.convert_button.setText("Remove backgrounds" if removing else "Convert images")

    def preview(self) -> None:
        if self.busy:
            return
        if not self.folder_input.text() or not self.output_input.text():
            self.status_label.setText("Select source and output folders")
            return
        folder = Path(self.folder_input.text())
        output = Path(self.output_input.text())
        removing = self.operation_combo.currentText() == "Remove background"
        output_format = "PNG" if removing else self.format_combo.currentText()
        options = ConversionOptions(output_format, self.quality_spin.value(), self.background,
                                    self.svg_spin.value(), self.first_frame_check.isChecked(), removing)
        input_format = self.input_combo.currentText()
        recursive = self.recursive_check.isChecked()
        self.invalidate()
        self._start_task(lambda progress: build_conversion_plan(folder, output, options, input_format,
                                                                recursive, progress), self._show_plan)

    def _show_plan(self, plan: ConversionPlan) -> None:
        self.plan = plan
        self.table.setRowCount(len(plan.entries))
        errors = sum(bool(entry.error) for entry in plan.entries)
        self.count_label.setText(f"{len(plan.entries)} images  |  {errors} errors")
        for row, entry in enumerate(plan.entries):
            values = (str(entry.source.relative_to(plan.folder)),
                      f"{entry.dimensions[0]} x {entry.dimensions[1]}",
                      str(entry.target.relative_to(plan.output_folder)), entry.error or "Ready")
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                if column == 0 and entry.thumbnail:
                    pixmap = QPixmap()
                    pixmap.loadFromData(entry.thumbnail)
                    item.setIcon(QIcon(pixmap))
                if column == 3:
                    item.setForeground(QColor("#bb3845" if entry.error else "#18765b"))
                self.table.setItem(row, column, item)
        self.status_label.setText(" ".join(plan.errors) or
                                  ("Resolve preview errors" if errors else
                                   "Ready to remove backgrounds" if plan.options.remove_background else
                                   "Ready to convert"))

    def convert(self) -> None:
        if self.busy or not self.plan or not self.plan.ready:
            return
        plan = self.plan
        self.cancel_event.clear()
        self._start_task(lambda progress: convert_batch(plan, progress, self.cancel_event.is_set), self._completed)
        self.cancel_button.setEnabled(True)

    def _completed(self, result: ConversionResult) -> None:
        if self.plan:
            for row, entry in enumerate(self.plan.entries):
                item = self.table.item(row, 3)
                done = "Background removed" if self.plan.options.remove_background else "Converted"
                status = (done if entry.target in result.outputs else
                          result.errors.get(entry.source, "Not converted"))
                item.setText(status)
                item.setToolTip(status)
                if entry.source in result.errors:
                    item.setForeground(QColor("#bb3845"))
        self.plan = None
        prefix = "Cancelled. " if result.cancelled else ""
        action = "Removed backgrounds from" if self.operation_combo.currentText() == "Remove background" else "Converted"
        self.status_label.setText(f"{prefix}{action} {len(result.outputs)} images; {len(result.errors)} errors")

    def _start_task(self, operation: Callable, on_success: Callable) -> None:
        self.busy = True
        self._on_success = on_success
        self.busy_changed.emit(True)
        for widget in self.input_widgets:
            widget.setEnabled(False)
        self.convert_button.setEnabled(False)
        self.progress.show()
        self.thread = QThread(self)
        self.worker = TaskWorker(operation)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.status_label.setText)
        self.worker.succeeded.connect(self._succeeded)
        self.worker.failed.connect(self._failed)
        self.worker.succeeded.connect(self.thread.quit)
        self.worker.failed.connect(self.thread.quit)
        self.worker.succeeded.connect(self.worker.deleteLater)
        self.worker.failed.connect(self.worker.deleteLater)
        self.thread.finished.connect(self._finished)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.start()

    def _succeeded(self, result: object) -> None:
        self._on_success(result)

    def _failed(self, message: str) -> None:
        self.plan = None
        self.status_label.setText(message)
        QMessageBox.warning(self, "Image conversion", message)

    def _finished(self) -> None:
        self.busy = False
        self.thread = None
        self.worker = None
        self.progress.hide()
        self.cancel_button.setEnabled(False)
        for widget in self.input_widgets:
            widget.setEnabled(True)
        self._update_option_states()
        self.convert_button.setEnabled(bool(self.plan and self.plan.ready))
        self.busy_changed.emit(False)
