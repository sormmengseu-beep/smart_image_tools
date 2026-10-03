from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtCore import QSettings, QSize, QStandardPaths, QThread, Qt, QUrl
from PySide6.QtGui import QCloseEvent, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QProgressBar, QPushButton, QStyle, QTableWidget, QTableWidgetItem,
    QTabWidget, QVBoxLayout, QWidget,
)

from app.workers import TaskWorker
from app.conversion_panel import ConversionPanel
from app.theme import (
    THEMES, GlassWorkspace, refresh_status_colors, style_status, theme_palette, theme_stylesheet,
)
from services.renamer import (
    FILE_TYPES, RenamePlan, RenameService, build_mapped_plan, build_plan,
    display_time, export_mapping, load_names, scan_files,
)


class MainWindow(QMainWindow):
    def __init__(self, history_folder: Path | None = None) -> None:
        super().__init__()
        self.settings = QSettings("SmartFileRenamer", "SmartFileRenamer")
        saved_theme = self.settings.value("appearance", "Dark")
        self.theme = saved_theme if isinstance(saved_theme, str) and saved_theme in THEMES else "Dark"
        self.setProperty("appearance", self.theme)
        self.service = RenameService(history_folder or Path(
            QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
        ) / "history")
        self.plan: RenamePlan | None = None
        self.thread: QThread | None = None
        self.worker: TaskWorker | None = None
        self.busy = False
        self.setWindowTitle("Smart File Renamer")
        self.setFont(QFont("Segoe UI", 10))
        self.setPalette(theme_palette(self.theme))
        self.setWindowIcon(self.style().standardIcon(QStyle.SP_FileDialogContentsView))
        self.resize(1160, 800)
        self.setMinimumSize(740, 600)
        self.setStyleSheet(theme_stylesheet(self.theme))
        self._build_ui()
        self._update_actions()

    def _button(self, text: str, icon: QStyle.StandardPixmap, callback: Callable) -> QPushButton:
        button = QPushButton(self.style().standardIcon(icon), text)
        button.setToolTip(text)
        button.setCursor(Qt.PointingHandCursor)
        button.clicked.connect(callback)
        return button

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(20, 14, 20, 14)
        layout.setSpacing(10)
        heading = QHBoxLayout()
        title = QLabel("Rename files")
        title.setObjectName("title")
        heading.addWidget(title)
        heading.addStretch()
        self.open_button = self._button("Open folder", QStyle.SP_DirOpenIcon, self.open_folder)
        heading.addWidget(self.open_button)
        layout.addLayout(heading)

        inputs = QGridLayout()
        inputs.setHorizontalSpacing(12)
        inputs.setVerticalSpacing(10)
        self.names_input = QLineEdit()
        self.names_input.setPlaceholderText("Names file (.txt or .csv)")
        self.names_input.setReadOnly(True)
        self.names_button = self._button("Load names", QStyle.SP_FileIcon, self.choose_names)
        self.folder_input = QLineEdit()
        self.folder_input.setPlaceholderText("Folder containing files to rename")
        self.folder_input.setReadOnly(True)
        self.folder_button = self._button("Select folder", QStyle.SP_DirIcon, self.choose_folder)
        inputs.addWidget(QLabel("Names file"), 0, 0)
        inputs.addWidget(self.names_input, 0, 1)
        inputs.addWidget(self.names_button, 0, 2)
        inputs.addWidget(QLabel("Folder"), 1, 0)
        inputs.addWidget(self.folder_input, 1, 1)
        inputs.addWidget(self.folder_button, 1, 2)
        inputs.setColumnStretch(1, 1)
        layout.addLayout(inputs)

        options = QGridLayout()
        options.setHorizontalSpacing(12)
        options.setVerticalSpacing(10)
        options.addWidget(QLabel("Files"), 0, 0)
        self.type_combo = QComboBox()
        self.type_combo.addItems(FILE_TYPES)
        options.addWidget(self.type_combo, 0, 1)
        options.addWidget(QLabel("Order"), 0, 2)
        self.order_combo = QComboBox()
        self.order_combo.addItems(["Natural name", "Alphabetical", "Oldest first", "Newest first"])
        self.order_combo.setCurrentText("Oldest first")
        options.addWidget(self.order_combo, 0, 3)
        self.date_combo = QComboBox()
        self.date_combo.addItems(["Modified", "Created"])
        options.addWidget(QLabel("Date"), 1, 0)
        options.addWidget(self.date_combo, 1, 1)
        self.recursive_check = QCheckBox("Include subfolders")
        self.extension_check = QCheckBox("Keep extensions")
        self.extension_check.setChecked(True)
        checks = QHBoxLayout()
        checks.setSpacing(16)
        checks.addWidget(self.recursive_check)
        checks.addWidget(self.extension_check)
        checks.addStretch()
        options.addLayout(checks, 1, 2, 1, 3)
        options.setColumnStretch(1, 1)
        options.setColumnStretch(3, 1)
        self.preview_button = self._button("Preview", QStyle.SP_BrowserReload, self.preview)
        options.addWidget(self.preview_button, 0, 4)
        options_content = QWidget()
        options_content.setMaximumWidth(940)
        options.setContentsMargins(0, 0, 0, 0)
        options_content.setLayout(options)
        layout.addWidget(options_content)

        summary = QHBoxLayout()
        self.count_label = QLabel("0 files  |  0 names  |  0 changes")
        self.count_label.setObjectName("counts")
        summary.addWidget(self.count_label)
        summary.addStretch()
        self.error_label = QLabel("")
        self.error_label.setObjectName("errors")
        summary.addWidget(self.error_label)
        layout.addLayout(summary)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["#", "Current file", "New file", "Modified time", "Status"])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(40)
        header = self.table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.Interactive)
        header.setSectionResizeMode(4, QHeaderView.Interactive)
        self.table.setColumnWidth(0, 50)
        self.table.setColumnWidth(3, 195)
        self.table.setColumnWidth(4, 160)
        self.table.cellDoubleClicked.connect(self.open_file)
        layout.addWidget(self.table, 1)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(4)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.status_label = QLabel("No folder selected")
        self.status_label.setObjectName("status")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        actions = QHBoxLayout()
        self.recover_button = self._button("Recover batch", QStyle.SP_BrowserReload, self.recover)
        self.undo_button = self._button("Undo last batch", QStyle.SP_ArrowBack, self.undo)
        self.export_button = self._button("Export mapping", QStyle.SP_DialogSaveButton, self.export_names)
        actions.addWidget(self.recover_button)
        actions.addWidget(self.undo_button)
        actions.addWidget(self.export_button)
        actions.addStretch()
        self.rename_button = self._button("Rename files", QStyle.SP_DialogApplyButton, self.rename)
        self.rename_button.setObjectName("primary")
        actions.addWidget(self.rename_button)
        layout.addLayout(actions)
        self.mode_tabs = QTabWidget()
        self.mode_tabs.setDocumentMode(True)
        self.mode_tabs.setIconSize(QSize(16, 16))
        self.converter = ConversionPanel(self)
        self.mode_tabs.addTab(root, self.style().standardIcon(QStyle.SP_FileDialogDetailedView), "Rename files")
        self.mode_tabs.addTab(self.converter, self.style().standardIcon(QStyle.SP_FileDialogContentsView), "Image tools")
        self.converter.busy_changed.connect(self._tab_busy)
        self.workspace = GlassWorkspace()
        self.workspace.set_theme(self.theme)
        shell_layout = QVBoxLayout(self.workspace)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)
        app_header = QFrame()
        app_header.setObjectName("appHeader")
        brand_layout = QHBoxLayout(app_header)
        brand_layout.setContentsMargins(20, 8, 20, 8)
        brand_layout.setSpacing(10)
        mark = QLabel()
        mark.setFixedSize(26, 26)
        mark.setPixmap(self.windowIcon().pixmap(24, 24))
        brand = QLabel("Smart File Renamer")
        brand.setObjectName("brand")
        brand_layout.addWidget(mark)
        brand_layout.addWidget(brand)
        brand_layout.addStretch()
        self.mode_tabs.setCornerWidget(app_header, Qt.TopLeftCorner)
        theme_controls = QFrame()
        theme_controls.setObjectName("themeControls")
        theme_layout = QHBoxLayout(theme_controls)
        theme_layout.setContentsMargins(12, 4, 16, 4)
        theme_layout.setSpacing(8)
        theme_label = QLabel("Theme")
        self.theme_combo = QComboBox()
        self.theme_combo.setAccessibleName("Color theme")
        self.theme_combo.setToolTip("Color theme")
        self.theme_combo.addItems(THEMES)
        self.theme_combo.setCurrentText(self.theme)
        self.theme_combo.setFixedWidth(96)
        theme_label.setBuddy(self.theme_combo)
        theme_layout.addWidget(theme_label)
        theme_layout.addWidget(self.theme_combo)
        self.mode_tabs.setCornerWidget(theme_controls, Qt.TopRightCorner)
        self.theme_combo.currentTextChanged.connect(self._apply_theme)
        shell_layout.addWidget(self.mode_tabs, 1)
        self.setCentralWidget(self.workspace)
        self.input_widgets = [self.names_button, self.folder_button, self.preview_button,
                              self.type_combo, self.order_combo, self.date_combo,
                              self.recursive_check, self.extension_check]
        for combo in (self.type_combo, self.order_combo, self.date_combo):
            combo.currentIndexChanged.connect(self._invalidate_preview)
        for check in (self.recursive_check, self.extension_check):
            check.toggled.connect(self._invalidate_preview)

    def _apply_theme(self, theme: str) -> None:
        if theme not in THEMES:
            return
        self.theme = theme
        self.setProperty("appearance", theme)
        self.setPalette(theme_palette(theme))
        self.setStyleSheet(theme_stylesheet(theme))
        self.workspace.set_theme(theme)
        refresh_status_colors(self.table)
        refresh_status_colors(self.converter.table)
        self.converter.background_editor._refresh()
        self.converter._update_background()
        self.settings.setValue("appearance", theme)

    def _update_actions(self) -> None:
        self.rename_button.setEnabled(not self.busy and bool(self.plan and self.plan.ready)
                                      and not self.service.needs_recovery)
        self.undo_button.setEnabled(not self.busy and self.service.can_undo)
        self.export_button.setEnabled(not self.busy and bool(self.plan and self.plan.entries))
        self.recover_button.setVisible(self.service.needs_recovery)
        self.recover_button.setEnabled(not self.busy)
        self.open_button.setEnabled(bool(self.folder_input.text()))
        if self.service.needs_recovery and not self.busy:
            self.status_label.setText("An interrupted batch needs recovery.")

    def _invalidate_preview(self, *_args) -> None:
        self.plan = None
        self.table.setRowCount(0)
        self.count_label.setText("0 files  |  0 names  |  0 changes")
        self.error_label.clear()
        self.status_label.setText("Preview pending")
        self._update_actions()

    def choose_names(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Load names file", self.settings.value("names_directory", ""),
            "Names files (*.txt *.csv);;Text files (*.txt);;CSV mappings (*.csv)"
        )
        if chosen:
            self.names_input.setText(chosen)
            self.names_input.setToolTip(chosen)
            self.settings.setValue("names_directory", str(Path(chosen).parent))
            self._invalidate_preview()
            if self.folder_input.text():
                self.preview()

    def choose_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Select folder to rename", self.settings.value("folder_directory", "")
        )
        if chosen:
            self.folder_input.setText(chosen)
            self.folder_input.setToolTip(chosen)
            self.settings.setValue("folder_directory", chosen)
            self._invalidate_preview()
            self.preview()

    def preview(self) -> None:
        if self.busy:
            return
        if not self.folder_input.text():
            self.status_label.setText("Select a folder.")
            return
        names_path = Path(self.names_input.text()) if self.names_input.text() else None
        folder = Path(self.folder_input.text())
        file_type = self.type_combo.currentText()
        order = self.order_combo.currentText()
        date_field = self.date_combo.currentText()
        recursive = self.recursive_check.isChecked()
        keep_extensions = self.extension_check.isChecked()
        self._invalidate_preview()

        def create_plan(progress: Callable) -> RenamePlan:
            progress("Reading names and scanning files...")
            files = scan_files(folder, file_type, recursive, order, exclude=names_path, date_field=date_field)
            if names_path and names_path.suffix.lower() == ".csv":
                return build_mapped_plan(folder, files, names_path, keep_extensions, date_field)
            names = load_names(names_path) if names_path else []
            return build_plan(folder, files, names, keep_extensions, date_field)

        self._start_task(create_plan, self._show_plan)

    def _show_plan(self, plan: RenamePlan) -> None:
        self.plan = plan
        self.table.horizontalHeaderItem(3).setText(f"{plan.date_field} time")
        self.table.setRowCount(len(plan.entries))
        row_errors = 0
        for row, entry in enumerate(plan.entries):
            current = str(entry.source.relative_to(plan.folder))
            try:
                target = str(entry.target.relative_to(plan.folder))
            except ValueError:
                target = str(entry.target)
            status = entry.error or ("Ready" if entry.changed else "Unchanged")
            if entry.error:
                row_errors += 1
            for column, value in enumerate((str(row + 1), current, target,
                                            display_time(entry.timestamp_ns), status)):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                if column == 4:
                    style_status(self.table, item, "error" if entry.error else "success" if entry.changed else "muted")
                self.table.setItem(row, column, item)
        self.count_label.setText(
            f"{len(plan.entries)} files  |  {plan.names_count} names  |  {len(plan.changes)} changes"
        )
        total_errors = row_errors + len(plan.errors)
        self.error_label.setText(f"{total_errors} errors" if total_errors else "")
        self.status_label.setText(" ".join(plan.errors) if plan.errors else
                                  f"{row_errors} filename conflicts" if row_errors else
                                  "Ready to rename" if plan.ready else "No changes needed")

    def rename(self) -> None:
        if self.busy or not self.plan or not self.plan.ready:
            return
        plan = self.plan
        self._start_task(lambda progress: self.service.apply(plan, progress),
                         lambda count: self._completed(count, "Renamed"))

    def undo(self) -> None:
        if not self.busy:
            self._start_task(self.service.undo, lambda count: self._completed(count, "Restored"))

    def recover(self) -> None:
        if not self.busy:
            self._start_task(self.service.recover, lambda count: self._completed(count, "Recovered"))

    def _completed(self, count: int, action: str) -> None:
        if action == "Renamed" and self.plan:
            for row, entry in enumerate(self.plan.entries):
                self.table.item(row, 4).setText("Renamed" if entry.changed else "Unchanged")
        else:
            self.table.setRowCount(0)
            self.count_label.setText("0 files  |  0 names  |  0 changes")
        self.plan = None
        self.error_label.clear()
        self.status_label.setText(f"{action} {count} files")

    def _start_task(self, operation: Callable, on_success: Callable) -> None:
        if self.busy:
            return
        self.busy = True
        self._tab_busy(True)
        for widget in self.input_widgets:
            widget.setEnabled(False)
        self.progress.show()
        self._update_actions()
        self.thread = QThread(self)
        self.worker = TaskWorker(operation)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.status_label.setText)
        self.worker.succeeded.connect(on_success)
        self.worker.failed.connect(self._failed)
        self.worker.succeeded.connect(self.thread.quit)
        self.worker.failed.connect(self.thread.quit)
        self.worker.succeeded.connect(self.worker.deleteLater)
        self.worker.failed.connect(self.worker.deleteLater)
        self.thread.finished.connect(self._task_finished)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.start()

    def _failed(self, message: str) -> None:
        self.plan = None
        self.table.setRowCount(0)
        self.count_label.setText("0 files  |  0 names  |  0 changes")
        self.error_label.setText("Operation failed")
        self.status_label.setText(message)
        QMessageBox.warning(self, "Smart File Renamer", message)

    def _task_finished(self) -> None:
        self.busy = False
        self._tab_busy(False)
        self.thread = None
        self.worker = None
        self.progress.hide()
        for widget in self.input_widgets:
            widget.setEnabled(True)
        self._update_actions()

    def open_folder(self) -> None:
        if self.folder_input.text():
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.folder_input.text()))

    def open_file(self, row: int, _column: int) -> None:
        if self.plan and not self.busy:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.plan.entries[row].source)))

    def export_names(self) -> None:
        if not self.plan or self.busy:
            return
        chosen, _ = QFileDialog.getSaveFileName(self, "Export filename mapping", "file_mapping.csv", "CSV (*.csv)")
        if not chosen:
            return
        try:
            export_mapping(self.plan, Path(chosen), self.extension_check.isChecked())
        except Exception as exc:
            QMessageBox.warning(self, "Export mapping", str(exc))
            return
        self.names_input.setText(chosen)
        self._invalidate_preview()
        self.status_label.setText(f"Saved mapping: {chosen}")

    def _tab_busy(self, busy: bool) -> None:
        self.mode_tabs.tabBar().setEnabled(not busy)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.busy or self.converter.busy:
            self.status_label.setText("An operation is in progress. The window can close when it finishes.")
            event.ignore()
        else:
            event.accept()
