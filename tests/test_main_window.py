from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication

from app.main_window import MainWindow


class MainWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        if os.name == "nt" and not QFontDatabase.families():
            fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
            for name in ("segoeui.ttf", "segoeuib.ttf"):
                QFontDatabase.addApplicationFont(str(fonts / name))

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.folder = self.root / "files"
        self.folder.mkdir()
        (self.folder / "photo.jpg").write_bytes(b"image contents")
        (self.folder / "video.mp4").write_bytes(b"video contents")
        self.names = self.root / "names.txt"
        self.names.write_text("Vacation\nCelebration\n", encoding="utf-8")
        self.window = MainWindow(self.root / "history")
        self.window.names_input.setText(str(self.names))
        self.window.folder_input.setText(str(self.folder))

    def tearDown(self):
        if self.window.busy:
            self.wait_for_task()
        if self.window.converter.busy:
            self.wait_for_task(self.window.converter)
        self.window.close()
        self.app.processEvents()
        self.temporary.cleanup()

    def wait_for_task(self, panel=None):
        panel = panel or self.window
        loop = QEventLoop()
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        timer.start(5000)
        panel.thread.finished.connect(loop.quit)
        loop.exec()
        self.app.processEvents()
        self.assertFalse(panel.busy, "Background task did not finish")

    def test_preview_rename_and_undo_through_worker(self):
        self.assertFalse(self.window.rename_button.isEnabled())
        self.window.preview()
        self.wait_for_task()
        self.assertEqual(self.window.table.rowCount(), 2)
        self.assertEqual(self.window.table.item(0, 2).text(), "Vacation.jpg")
        self.assertTrue(self.window.rename_button.isEnabled())
        self.window.rename()
        self.wait_for_task()
        self.assertEqual(self.window.status_label.text(), "Renamed 2 files")
        self.assertTrue((self.folder / "Celebration.mp4").exists())
        self.assertFalse(self.window.rename_button.isEnabled())
        self.assertTrue(self.window.undo_button.isEnabled())
        self.window.undo()
        self.wait_for_task()
        self.assertTrue((self.folder / "photo.jpg").exists())
        self.assertTrue((self.folder / "video.mp4").exists())
        self.assertFalse(self.window.undo_button.isEnabled())

    def test_filter_change_invalidates_previous_preview(self):
        self.window.preview()
        self.wait_for_task()
        self.window.type_combo.setCurrentText("Images")
        self.assertIsNone(self.window.plan)
        self.assertFalse(self.window.rename_button.isEnabled())
        self.window.preview()
        self.wait_for_task()
        self.assertEqual(self.window.table.rowCount(), 1)
        self.assertIn("Counts must match", self.window.status_label.text())
        self.assertFalse(self.window.rename_button.isEnabled())

    @patch("app.main_window.QMessageBox.warning")
    def test_missing_names_file_leaves_no_stale_plan(self, warning):
        self.window.preview()
        self.wait_for_task()
        self.names.unlink()
        with self.assertLogs("app.workers", level="ERROR"):
            self.window.preview()
            self.wait_for_task()
        self.assertIsNone(self.window.plan)
        self.assertFalse(self.window.rename_button.isEnabled())
        self.assertEqual(self.window.table.rowCount(), 0)
        warning.assert_called_once()

    def test_controls_fit_compact_window(self):
        self.window.resize(740, 520)
        self.window.show()
        self.app.processEvents()
        self.assertLessEqual(self.window.centralWidget().minimumSizeHint().width(), 740)
        for widget in self.window.input_widgets:
            self.assertGreaterEqual(widget.width(), widget.minimumSizeHint().width())

    def test_conversion_preview_and_batch_through_worker(self):
        image = self.folder / "photo.jpg"
        Image.new("RGB", (60, 30), "red").save(image)
        (self.folder / "graphic.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50">'
            '<rect width="100" height="50" fill="red"/></svg>', encoding="utf-8"
        )
        original = image.read_bytes()
        panel = self.window.converter
        self.window.mode_tabs.setCurrentIndex(1)
        panel.folder_input.setText(str(self.folder))
        panel.output_input.setText(str(self.folder / "converted"))
        panel.format_combo.setCurrentText("PNG")
        panel.preview()
        self.assertFalse(self.window.mode_tabs.tabBar().isEnabled())
        self.wait_for_task(panel)
        self.assertEqual(panel.table.rowCount(), 2)
        self.assertTrue(panel.convert_button.isEnabled())
        self.assertFalse(panel.table.item(0, 0).icon().isNull())
        panel.convert()
        self.wait_for_task(panel)
        self.assertEqual(panel.status_label.text(), "Converted 2 images; 0 errors")
        self.assertEqual(panel.table.item(0, 3).text(), "Converted")
        self.assertEqual(image.read_bytes(), original)
        with Image.open(self.folder / "converted/photo.png") as converted:
            self.assertEqual(converted.format, "PNG")
        with Image.open(self.folder / "converted/graphic.png") as converted:
            self.assertEqual(converted.size, (2048, 1024))
        self.assertFalse(panel.convert_button.isEnabled())
        self.assertTrue(self.window.mode_tabs.tabBar().isEnabled())

    def test_conversion_options_fit_compact_window(self):
        self.window.mode_tabs.setCurrentIndex(1)
        self.window.resize(740, 520)
        self.window.show()
        self.app.processEvents()
        panel = self.window.converter
        self.assertLessEqual(panel.minimumSizeHint().width(), 740)
        for widget in panel.input_widgets:
            minimum = (widget.minimumWidth() if widget.minimumWidth() == widget.maximumWidth()
                       else widget.minimumSizeHint().width())
            self.assertGreaterEqual(widget.width(), minimum)

    def test_folder_only_preview_can_export_mapping_without_renaming(self):
        self.window.names_input.clear()
        self.window.preview()
        self.wait_for_task()
        self.assertEqual(self.window.table.rowCount(), 2)
        self.assertTrue(self.window.export_button.isEnabled())
        self.assertFalse(self.window.rename_button.isEnabled())
        destination = self.root / "mapping.csv"
        with patch("app.main_window.QFileDialog.getSaveFileName", return_value=(str(destination), "CSV")):
            self.window.export_names()
        self.assertTrue(destination.exists())
        self.window.preview()
        self.wait_for_task()
        self.assertFalse(self.window.rename_button.isEnabled())
        self.assertFalse(self.window.plan.errors)
        self.assertTrue(all(not entry.changed for entry in self.window.plan.entries))

    def test_timestamp_and_date_change_invalidate_name_assignment(self):
        self.window.preview()
        self.wait_for_task()
        self.assertEqual(self.window.table.horizontalHeaderItem(3).text(), "Modified time")
        self.assertRegex(self.window.table.item(0, 3).text(), r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
        self.window.date_combo.setCurrentText("Created")
        self.assertIsNone(self.window.plan)
        self.assertFalse(self.window.rename_button.isEnabled())


if __name__ == "__main__":
    unittest.main()
