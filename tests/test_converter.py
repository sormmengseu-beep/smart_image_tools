from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtWidgets import QApplication

from services.converter import (
    OUTPUT_FORMATS, ConversionError, ConversionOptions,
    _ensure_background_remover, build_conversion_plan, convert_batch,
)


class ConversionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.folder = self.root / "images"
        self.folder.mkdir()
        self.output = self.folder / "converted"

    def image(self, name="image.png", color=(200, 30, 50, 255), size=(20, 10)):
        path = self.folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGBA", size, color)
        if path.suffix.lower() in {".jpg", ".jpeg", ".heic"}:
            image = image.convert("RGB")
        image.save(path)
        return path

    def plan(self, **kwargs):
        return build_conversion_plan(self.folder, self.output, ConversionOptions(**kwargs))

    def test_raster_conversion_outputs_are_real_formats(self):
        source = self.image()
        original = source.read_bytes()
        for label, (format_name, suffix) in OUTPUT_FORMATS.items():
            with self.subTest(format=label):
                plan = self.plan(output_format=label)
                self.assertTrue(plan.ready)
                result = convert_batch(plan)
                self.assertFalse(result.errors)
                with Image.open(self.output / f"image{suffix}") as image:
                    self.assertEqual(image.format, format_name)
                    self.assertEqual(image.size, (20, 10))
        self.assertEqual(source.read_bytes(), original)

    def test_webp_to_jpg_and_mistyped_webpg_extension(self):
        source = self.image("image.webp")
        renamed = source.with_suffix(".webpg")
        source.rename(renamed)
        plan = build_conversion_plan(self.folder, self.output, ConversionOptions(), "WebP")
        self.assertTrue(plan.ready)
        self.assertEqual(len(convert_batch(plan).outputs), 1)
        with Image.open(self.output / "image.jpg") as image:
            self.assertEqual(image.format, "JPEG")
        self.assertTrue(renamed.exists())

    def test_svg_rasterization_and_transparency(self):
        source = self.folder / "shape.svg"
        source.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50">'
            '<rect x="0" y="0" width="50" height="50" fill="#ff0000"/></svg>', encoding="utf-8"
        )
        result = convert_batch(self.plan(output_format="PNG", svg_width=128))
        self.assertFalse(result.errors)
        with Image.open(self.output / "shape.png") as image:
            self.assertEqual(image.size, (128, 64))
            self.assertEqual(image.getpixel((10, 10)), (255, 0, 0, 255))
            self.assertEqual(image.getpixel((120, 10))[3], 0)
        result = convert_batch(self.plan(svg_width=128))
        self.assertFalse(result.errors)
        with Image.open(self.output / "shape.jpg") as image:
            self.assertTrue(all(value >= 250 for value in image.getpixel((120, 10))))

    def test_transparency_flattened_with_background_and_preserved_for_png(self):
        self.image("transparent.webp", color=(255, 0, 0, 0))
        result = convert_batch(self.plan(background="#000000"))
        self.assertFalse(result.errors)
        with Image.open(self.output / "transparent.jpg") as image:
            self.assertTrue(all(value <= 5 for value in image.getpixel((5, 5))))
        result = convert_batch(self.plan(output_format="PNG"))
        self.assertFalse(result.errors)
        with Image.open(self.output / "transparent.png") as image:
            self.assertEqual(image.getpixel((5, 5))[3], 0)

    def test_background_removal_outputs_transparent_pngs(self):
        self.image("subject.jpg", color=(10, 20, 30, 255))

        def transparent_result(image):
            return Image.new("RGBA", image.size, (0, 0, 0, 0))

        options = ConversionOptions(output_format="JPG", remove_background=True)
        with patch("services.converter._ensure_background_remover"), \
                patch("services.converter._remove_background", side_effect=transparent_result):
            plan = build_conversion_plan(self.folder, self.output, options)
            self.assertTrue(plan.ready)
            self.assertEqual(plan.entries[0].target.name, "subject.png")
            result = convert_batch(plan)

        self.assertFalse(result.errors)
        with Image.open(self.output / "subject.png") as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.getpixel((5, 5))[3], 0)

    def test_background_removal_requires_dependency_at_preview(self):
        self.image()
        with patch("services.converter._ensure_background_remover",
                   side_effect=ConversionError("Background removal requires rembg")):
            with self.assertRaisesRegex(ConversionError, "rembg"):
                self.plan(remove_background=True)

    def test_background_dependency_error_uses_app_python_and_cpu_extra(self):
        original_import = __import__

        def missing_dependency(name, *args, **kwargs):
            if name == "rembg":
                raise ModuleNotFoundError("No module named 'onnxruntime'", name="onnxruntime")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=missing_dependency):
            with self.assertRaises(ConversionError) as caught:
                _ensure_background_remover()
        message = str(caught.exception)
        self.assertIn(sys.executable, message)
        self.assertIn("rembg[cpu]", message)
        self.assertIn("onnxruntime", message)

    def test_background_runtime_exit_becomes_a_reportable_error(self):
        original_import = __import__

        def missing_runtime(name, *args, **kwargs):
            if name == "rembg":
                raise SystemExit(1)
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=missing_runtime):
            with self.assertRaisesRegex(ConversionError, "CPU runtime unavailable"):
                _ensure_background_remover()

    def test_exif_orientation_applied_once_and_metadata_preserved(self):
        source = self.folder / "rotated.jpg"
        exif = Image.Exif()
        exif[274] = 6
        exif[306] = "2025:01:02 03:04:05"
        Image.new("RGB", (20, 10), "red").save(source, exif=exif)
        result = convert_batch(self.plan(output_format="PNG"))
        self.assertFalse(result.errors)
        with Image.open(self.output / "rotated.png") as image:
            self.assertEqual(image.size, (10, 20))
            self.assertNotIn(274, image.getexif())
            self.assertEqual(image.getexif()[306], "2025:01:02 03:04:05")
        with Image.open(source) as original:
            self.assertEqual(original.size, (20, 10))
            self.assertEqual(original.getexif()[274], 6)

    def test_modification_time_preserved_for_date_sorting(self):
        source = self.image()
        os.utime(source, ns=(1_600_000_000_123456700, 1_600_000_000_123456700))
        result = convert_batch(self.plan())
        self.assertFalse(result.errors)
        self.assertEqual(result.outputs[0].stat().st_mtime_ns, source.stat().st_mtime_ns)

    def test_heic_input_supported(self):
        self.image("photo.heic")
        plan = self.plan()
        self.assertTrue(plan.ready)
        self.assertFalse(convert_batch(plan).errors)
        with Image.open(self.output / "photo.jpg") as image:
            self.assertEqual(image.format, "JPEG")

    def test_animated_images_require_explicit_first_frame(self):
        source = self.folder / "animated.gif"
        Image.new("RGB", (20, 10), "red").save(
            source, save_all=True, append_images=[Image.new("RGB", (20, 10), "blue")], duration=100, loop=0
        )
        plan = self.plan()
        self.assertFalse(plan.ready)
        self.assertIn("First frame", plan.entries[0].error)
        plan = self.plan(first_frame=True)
        self.assertTrue(plan.ready)
        self.assertFalse(convert_batch(plan).errors)

    def test_duplicate_output_names_blocked(self):
        self.image("image.png")
        self.image("image.webp")
        plan = self.plan()
        self.assertFalse(plan.ready)
        self.assertTrue(all("Duplicate" in entry.error for entry in plan.entries))
        with self.assertRaises(ConversionError):
            convert_batch(plan)

    def test_existing_output_blocked_at_preview(self):
        self.image()
        self.output.mkdir()
        target = self.output / "IMAGE.JPG"
        target.write_bytes(b"do not replace")
        plan = self.plan()
        self.assertFalse(plan.ready)
        self.assertIn("exists", plan.entries[0].error)
        self.assertEqual(target.read_bytes(), b"do not replace")

    def test_output_added_after_preview_is_never_overwritten(self):
        self.image()
        plan = self.plan()
        self.output.mkdir()
        target = self.output / "image.jpg"
        target.write_bytes(b"existing data")
        result = convert_batch(plan)
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(target.read_bytes(), b"existing data")
        self.assertEqual(list(self.output.iterdir()), [target])

    def test_changed_input_blocks_batch_before_creating_outputs(self):
        source = self.image()
        plan = self.plan()
        source.write_bytes(b"changed file")
        with self.assertRaisesRegex(ConversionError, "changed"):
            convert_batch(plan)
        self.assertFalse(self.output.exists())

    def test_subfolders_preserved_and_output_excluded_from_source_scan(self):
        self.image("nested/source.png")
        self.image("converted/previous.png")
        plan = build_conversion_plan(self.folder, self.output, ConversionOptions(), recursive=True)
        self.assertEqual(len(plan.entries), 1)
        self.assertFalse(convert_batch(plan).errors)
        self.assertTrue((self.output / "nested/source.jpg").exists())

    def test_invalid_svg_and_corrupt_image_are_preview_errors(self):
        (self.folder / "bad.svg").write_text("not SVG", encoding="utf-8")
        (self.folder / "bad.jpg").write_bytes(b"not a JPEG")
        plan = self.plan()
        self.assertFalse(plan.ready)
        self.assertTrue(all(entry.error for entry in plan.entries))

    def test_cancel_keeps_completed_outputs_and_originals(self):
        self.image("a.png")
        self.image("b.png")
        calls = iter((False, True))
        result = convert_batch(self.plan(), cancelled=lambda: next(calls))
        self.assertTrue(result.cancelled)
        self.assertEqual(len(result.outputs), 1)
        self.assertTrue((self.folder / "a.png").exists())
        self.assertTrue((self.folder / "b.png").exists())
        self.assertFalse((self.output / "b.jpg").exists())

    def test_failure_does_not_leave_partial_file(self):
        self.image()
        with patch("services.converter._save_image", side_effect=OSError("encoder failed")):
            result = convert_batch(self.plan())
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertTrue((self.folder / "image.png").exists())

    def test_source_and_output_folder_must_differ(self):
        self.image()
        with self.assertRaisesRegex(ConversionError, "separate"):
            build_conversion_plan(self.folder, self.folder, ConversionOptions())


if __name__ == "__main__":
    unittest.main()
