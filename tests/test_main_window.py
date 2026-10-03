from __future__ import annotations

import os
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QEventLoop, QSettings, QTimer
from PySide6.QtGui import QColor, QFontDatabase, QIcon
from PySide6.QtWidgets import QApplication

from app.main_window import MainWindow
from app.background_editor import BackgroundEditor


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
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.DeferredDelete)
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

    def test_add_background_batch_and_palette_change_invalidates_preview(self):
        Image.new("RGBA", (60, 30), (255, 0, 0, 0)).save(self.folder / "cutout.png")
        panel = self.window.converter
        panel.folder_input.setText(str(self.folder))
        panel.output_input.setText(str(self.folder / "colored"))
        panel.operation_combo.setCurrentText("Add background")
        panel.input_combo.setCurrentText("PNG")
        panel.format_combo.setCurrentText("PNG")
        editor = panel.background_editor
        editor.preset_combo.setCurrentText("Sky")
        editor.apply_preset()
        panel.preview()
        self.wait_for_task(panel)
        self.assertTrue(panel.convert_button.isEnabled())
        self.assertEqual(panel.plan.options.background_mode, "Linear gradient")
        editor.swap_colors()
        self.assertIsNone(panel.plan)
        self.assertFalse(panel.convert_button.isEnabled())
        panel.preview()
        self.wait_for_task(panel)
        panel.convert()
        self.wait_for_task(panel)
        self.assertEqual(panel.status_label.text(), "Added backgrounds to 1 images; 0 errors")
        self.assertEqual(panel.table.item(0, 3).text(), "Background added")
        with Image.open(self.folder / "colored/cutout.png") as output:
            self.assertEqual(output.getchannel("A").getextrema(), (255, 255))
            self.assertNotEqual(output.getpixel((0, 0)), output.getpixel((0, 29)))

    def test_background_presets_survive_restart_and_can_be_deleted(self):
        settings = QSettings(str(self.root / "settings.ini"), QSettings.IniFormat)
        editor = BackgroundEditor(settings)
        editor.setParent(self.window)
        editor.preset_combo.setCurrentText("Sunset")
        editor.apply_preset()
        with patch("app.background_editor.QInputDialog.getText", return_value=("My palette", True)):
            editor.save_preset()
        settings.sync()
        restored = BackgroundEditor(QSettings(str(self.root / "settings.ini"), QSettings.IniFormat))
        restored.setParent(self.window)
        restored.preset_combo.setCurrentText("My palette")
        restored.apply_preset()
        self.assertEqual(restored.options(), editor.options())
        self.assertTrue(restored.delete_button.isEnabled())
        restored.delete_preset()
        self.assertNotIn("My palette", restored.saved_presets)
        restored.settings.sync()
        reloaded = BackgroundEditor(settings)
        reloaded.setParent(self.window)
        self.assertNotIn("My palette", reloaded.saved_presets)

    def test_random_background_custom_canvas_batch_and_shuffle_invalidation(self):
        for name in ("one.png", "two.png"):
            Image.new("RGBA", (60, 30), (0, 0, 0, 0)).save(self.folder / name)
        panel = self.window.converter
        panel.folder_input.setText(str(self.folder))
        panel.output_input.setText(str(self.folder / "randomized"))
        panel.operation_combo.setCurrentText("Add background")
        panel.input_combo.setCurrentText("PNG")
        panel.format_combo.setCurrentText("PNG")
        editor = panel.background_editor
        editor.canvas_combo.setCurrentText("Custom")
        editor.width_spin.setValue(90)
        editor.height_spin.setValue(60)
        editor.mode_combo.setCurrentText("Random radial gradient")
        self.assertTrue(editor.shuffle_button.isEnabled())
        self.assertTrue(editor.start_button.isEnabled())
        self.assertFalse(editor.end_button.isEnabled())
        with patch("app.background_editor.QColorDialog.getColor", return_value=QColor("#663399")):
            editor.start_button.click()
        panel.preview()
        self.wait_for_task(panel)
        self.assertTrue(panel.convert_button.isEnabled())
        first_options = panel.plan.entries[0].options
        self.assertEqual(first_options.background_mode, "Radial gradient")
        self.assertEqual(panel.table.item(0, 1).text(), "90 x 60")
        self.assertEqual(panel.table.horizontalHeaderItem(0).text(), "Image preview")
        for row, entry in enumerate(panel.plan.entries):
            self.assertEqual(entry.options.background, "#663399")
            with Image.open(io.BytesIO(entry.preview_thumbnail)) as preview:
                icon = panel.table.item(row, 0).icon()
                for mode in (QIcon.Normal, QIcon.Selected):
                    displayed = icon.pixmap(56, 56, mode).toImage()
                    self.assertEqual(displayed.pixelColor(0, 0).getRgb(), preview.getpixel((0, 0)))
        panel.table.setCurrentCell(1, 0)
        self.assertEqual(editor.preview_options, panel.plan.entries[1].options)
        editor.shuffle_backgrounds()
        self.assertIsNone(panel.plan)
        self.assertFalse(panel.convert_button.isEnabled())
        panel.preview()
        self.wait_for_task(panel)
        shuffled_options = panel.plan.entries[0].options
        self.assertEqual(first_options.background, shuffled_options.background)
        self.assertNotEqual(first_options.background_end, shuffled_options.background_end)
        self.assertEqual(first_options.gradient_center, shuffled_options.gradient_center)
        panel.convert()
        self.wait_for_task(panel)
        self.assertEqual(panel.status_label.text(), "Added backgrounds to 2 images; 0 errors")
        with Image.open(self.folder / "randomized/one.png") as output:
            self.assertEqual(output.size, (90, 60))
            self.assertEqual(output.getchannel("A").getextrema(), (255, 255))

    def test_changing_shared_radial_color_updates_rows_and_export_without_preview(self):
        for name in ("one.png", "two.png", "three.png"):
            Image.new("RGBA", (256, 256), (0, 0, 0, 0)).save(self.folder / name)
        panel = self.window.converter
        panel.folder_input.setText(str(self.folder))
        panel.output_input.setText(str(self.folder / "recolored"))
        panel.operation_combo.setCurrentText("Add background")
        panel.input_combo.setCurrentText("PNG")
        panel.format_combo.setCurrentText("PNG")
        editor = panel.background_editor
        editor.mode_combo.setCurrentText("Random radial gradient")
        panel.preview()
        self.wait_for_task(panel)
        original_ends = [entry.options.background_end for entry in panel.plan.entries]
        self.assertGreater(len(set(original_ends)), 1)
        panel.table.setCurrentCell(1, 0)
        with patch("app.background_editor.QColorDialog.getColor", return_value=QColor("#ff0000")):
            editor.start_button.click()
        self.assertFalse(panel.busy)
        self.assertIsNotNone(panel.plan)
        self.assertEqual(panel.table.rowCount(), 3)
        self.assertEqual(panel.table.currentRow(), 1)
        self.assertTrue(panel.convert_button.isEnabled())
        self.assertEqual([entry.options.background_end for entry in panel.plan.entries], original_ends)
        for row, entry in enumerate(panel.plan.entries):
            self.assertEqual(entry.options.background, "#ff0000")
            self.assertEqual(entry.options.background_mode, "Radial gradient")
            center = panel.table.item(row, 0).icon().pixmap(56, 56).toImage().pixelColor(28, 28)
            self.assertGreaterEqual(center.red(), 249)
            self.assertLessEqual(center.green(), 6)
            self.assertLessEqual(center.blue(), 6)
        panel.convert()
        self.wait_for_task(panel)
        for name in ("one.png", "two.png", "three.png"):
            with Image.open(self.folder / "recolored" / name) as output:
                center = output.getpixel((128, 128))
                self.assertGreaterEqual(center[0], 253)
                self.assertLessEqual(center[1], 3)
                self.assertLessEqual(center[2], 3)

    def test_random_gradient_presets_save_palette_and_keep_random_style(self):
        settings = QSettings(str(self.root / "random-presets.ini"), QSettings.IniFormat)
        editor = BackgroundEditor(settings)
        editor.setParent(self.window)
        editor.mode_combo.setCurrentText("Random linear gradient")
        editor.preset_combo.setCurrentText("Sunset")
        editor.apply_preset()
        self.assertEqual(editor.mode_combo.currentText(), "Random linear gradient")
        self.assertTrue(editor.save_button.isEnabled())
        with patch("app.background_editor.QInputDialog.getText", return_value=("My random gradient", True)):
            editor.save_preset()
        settings.sync()
        restored = BackgroundEditor(QSettings(str(self.root / "random-presets.ini"), QSettings.IniFormat))
        restored.setParent(self.window)
        restored.preset_combo.setCurrentText("My random gradient")
        restored.apply_preset()
        self.assertEqual(restored.options().background_distribution, "Random linear gradient")
        self.assertEqual(restored.options().background, editor.options().background)
        self.assertEqual(restored.options().background_end, editor.options().background_end)

    def test_add_background_controls_fit_compact_window(self):
        self.window.mode_tabs.setCurrentIndex(1)
        panel = self.window.converter
        panel.operation_combo.setCurrentText("Add background")
        self.window.resize(740, 600)
        self.window.show()
        self.app.processEvents()
        self.assertLessEqual(panel.minimumSizeHint().width(), 740)
        self.assertTrue(panel.background_editor.isVisible())
        self.assertTrue(panel.format_combo.isEnabled())
        self.assertTrue(panel.remove_existing_check.isVisible())
        editor = panel.background_editor
        editor.mode_combo.setCurrentText("Random linear gradient")
        self.app.processEvents()
        for widget in (editor.mode_combo, editor.preset_combo, editor.direction_combo):
            self.assertGreaterEqual(widget.width(), widget.minimumSizeHint().width())
            self.assertGreaterEqual(widget.height(), 36)
        for widget in (panel.operation_combo, panel.input_combo, panel.quality_spin):
            self.assertGreaterEqual(widget.height(), 36)
        editor.canvas_combo.setCurrentText("Custom")
        self.app.processEvents()
        for widget in (editor.canvas_combo, editor.width_spin, editor.height_spin, editor.fit_combo):
            self.assertGreaterEqual(widget.height(), 36)
            self.assertGreaterEqual(widget.width(), widget.minimumSizeHint().width())
            self.assertLessEqual(widget.geometry().bottom(), editor.height())
        panel.operation_combo.setCurrentText("Remove background")
        self.assertFalse(editor.isVisible())
        self.assertFalse(panel.format_combo.isEnabled())

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
