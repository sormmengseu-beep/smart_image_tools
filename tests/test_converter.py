from __future__ import annotations

import os
import io
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image, ImageColor
from PySide6.QtWidgets import QApplication

from services.converter import (
    OUTPUT_FORMATS, ConversionError, ConversionOptions,
    _batch_random_colors, _ensure_background_remover, background_layer,
    build_conversion_plan, convert_batch, recolor_plan,
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

    def test_solid_background_composites_alpha_and_keeps_subject(self):
        source = self.image(color=(255, 0, 0, 0))
        with Image.open(source) as image:
            image.putpixel((1, 1), (255, 0, 0, 255))
            image.putpixel((2, 1), (255, 0, 0, 128))
            image.save(source)
        original = source.read_bytes()
        result = convert_batch(self.plan(output_format="PNG", background_mode="Solid",
                                         background="#0000ff"))
        self.assertFalse(result.errors)
        with Image.open(result.outputs[0]) as output:
            self.assertEqual(output.getpixel((0, 0)), (0, 0, 255, 255))
            self.assertEqual(output.getpixel((1, 1)), (255, 0, 0, 255))
            self.assertEqual(output.getpixel((2, 1)), (128, 0, 127, 255))
        self.assertEqual(source.read_bytes(), original)

    def test_linear_gradient_directions_and_endpoints(self):
        for direction in ("Horizontal", "Vertical", "Diagonal"):
            with self.subTest(direction=direction):
                options = ConversionOptions(background_mode="Linear gradient", background="#000000",
                                            background_end="#ffffff", gradient_direction=direction)
                image = background_layer((11, 11), options)
                self.assertEqual(image.getpixel((0, 0)), (0, 0, 0, 255))
                self.assertEqual(image.getpixel((10, 10)), (255, 255, 255, 255))
                self.assertTrue(126 <= image.getpixel((5, 5))[0] <= 129)
                if direction == "Horizontal":
                    self.assertEqual(image.getpixel((0, 10)), (0, 0, 0, 255))
                if direction == "Vertical":
                    self.assertEqual(image.getpixel((10, 0)), (0, 0, 0, 255))

    def test_gradient_angles_render_in_both_directions(self):
        expected = {0: (0, 255, 0, 255), 90: (0, 0, 255, 255),
                    180: (255, 0, 255, 0), 270: (255, 255, 0, 0)}
        for angle, corners in expected.items():
            with self.subTest(angle=angle):
                image = background_layer((11, 11), ConversionOptions(
                    background_mode="Linear gradient", background="#000000", background_end="#ffffff",
                    gradient_angle=angle))
                actual = [image.getpixel(point)[0] for point in ((0, 0), (10, 0), (0, 10), (10, 10))]
                self.assertTrue(all(abs(value - target) <= 1 for value, target in zip(actual, corners)))

    def test_radial_gradient_moves_center_without_changing_colors(self):
        options = ConversionOptions(background_mode="Radial gradient", background="#ffffff",
                                    background_end="#000000", gradient_center=(0.2, 0.8))
        image = background_layer((101, 101), options)
        self.assertGreater(image.getpixel((20, 80))[0], 250)
        self.assertEqual(image.getpixel((100, 0)), (0, 0, 0, 255))
        shifted = background_layer((101, 101), replace(options, gradient_center=(0.8, 0.2)))
        self.assertGreater(shifted.getpixel((80, 20))[0], 250)
        self.assertLess(shifted.getpixel((20, 80))[0], 5)

    def test_radial_gradient_is_light_at_center_and_dark_at_edges(self):
        self.image(color=(0, 0, 0, 0), size=(256, 256))
        result = convert_batch(self.plan(output_format="PNG", background_mode="Radial gradient",
                                         background="#ffffff", background_end="#000000"))
        self.assertFalse(result.errors)
        with Image.open(result.outputs[0]) as output:
            self.assertGreater(output.getpixel((128, 128))[0], 250)
            self.assertLess(output.getpixel((0, 0))[0], 5)
            self.assertEqual(output.getchannel("A").getextrema(), (255, 255))

    def test_remove_then_add_background_uses_selected_output_format(self):
        self.image("subject.jpg")
        for label in ("PNG", "JPG", "WebP"):
            with self.subTest(format=label), patch("services.converter._ensure_background_remover"), \
                    patch("services.converter._remove_background", side_effect=lambda image:
                          Image.new("RGBA", image.size, (0, 0, 0, 0))) as remove:
                plan = self.plan(output_format=label, remove_background=True,
                                 background_mode="Solid", background="#00ff00")
                self.assertEqual(plan.entries[0].target.suffix, OUTPUT_FORMATS[label][1])
                result = convert_batch(plan)
                self.assertFalse(result.errors)
                remove.assert_called_once()
                with Image.open(result.outputs[0]) as output:
                    self.assertEqual(output.format, OUTPUT_FORMATS[label][0])
                    pixel = output.convert("RGB").getpixel((0, 0))
                    self.assertLessEqual(pixel[0], 3)
                    self.assertGreaterEqual(pixel[1], 252)
                    self.assertLessEqual(pixel[2], 3)

    def test_invalid_background_options_are_preview_errors(self):
        self.image()
        for options in ({"background_mode": "Unknown"}, {"background": "bad-color"},
                        {"background_end": "#ffffff00"}, {"gradient_direction": "Unknown"}):
            with self.subTest(options=options), self.assertRaises(ConversionError):
                self.plan(**options)

    def test_custom_canvas_centers_subject_without_distortion(self):
        source = self.image(color=(255, 0, 0, 255))
        original = source.read_bytes()
        result = convert_batch(self.plan(output_format="PNG", background_mode="Solid",
                                         background="#0000ff", canvas_width=40, canvas_height=40))
        self.assertFalse(result.errors)
        with Image.open(result.outputs[0]) as image:
            self.assertEqual(image.size, (40, 40))
            self.assertEqual(image.getpixel((20, 9)), (0, 0, 255, 255))
            self.assertEqual(image.getpixel((20, 10)), (255, 0, 0, 255))
            self.assertEqual(image.getpixel((20, 29)), (255, 0, 0, 255))
            self.assertEqual(image.getpixel((20, 30)), (0, 0, 255, 255))
        self.assertEqual(source.read_bytes(), original)

    def test_record_thumbnail_shows_custom_background_and_centered_subject(self):
        self.image(color=(255, 0, 0, 255))
        plan = self.plan(output_format="PNG", background_mode="Solid", background="#0000ff",
                         canvas_width=40, canvas_height=40)
        entry = plan.entries[0]
        with Image.open(io.BytesIO(entry.preview_thumbnail)) as preview:
            self.assertEqual(preview.size, (56, 56))
            self.assertEqual(preview.getpixel((0, 0)), (0, 0, 255, 255))
            self.assertEqual(preview.getpixel((28, 28)), (255, 0, 0, 255))
        with Image.open(io.BytesIO(entry.thumbnail)) as source:
            self.assertEqual(source.size, (20, 10))
            self.assertEqual(source.getpixel((0, 0)), (255, 0, 0, 255))

    def test_custom_canvas_fill_crops_proportionally_instead_of_letterboxing(self):
        self.image(color=(255, 0, 0, 255))
        result = convert_batch(self.plan(output_format="PNG", background_mode="Solid",
                                         background="#0000ff", canvas_width=30, canvas_height=30,
                                         canvas_fit="Fill and crop"))
        self.assertFalse(result.errors)
        with Image.open(result.outputs[0]) as image:
            self.assertEqual(image.size, (30, 30))
            self.assertEqual(image.getpixel((0, 0)), (255, 0, 0, 255))
            self.assertEqual(image.getpixel((29, 29)), (255, 0, 0, 255))

    def test_custom_canvas_jpeg_saves_actual_output_size(self):
        source = self.image(color=(0, 0, 0, 0))
        exif = Image.Exif()
        exif[256] = 20
        exif[257] = 10
        exif[34665] = {40962: 20, 40963: 10}
        with Image.open(source) as image:
            image.save(source, exif=exif)
        result = convert_batch(self.plan(output_format="JPG", background_mode="Solid",
                                         canvas_width=60, canvas_height=30))
        self.assertFalse(result.errors)
        with Image.open(result.outputs[0]) as image:
            self.assertEqual(image.size, (60, 30))
            self.assertEqual(image.getexif()[256], 60)
            self.assertEqual(image.getexif()[257], 30)
            dimensions = image.getexif().get_ifd(34665)
            self.assertEqual(dimensions[40962], 60)
            self.assertEqual(dimensions[40963], 30)

    def test_invalid_canvas_dimensions_are_blocked_before_allocating(self):
        self.image()
        for width, height in ((0, 20), (20, 0), (-1, 20), (20001, 1), (10000, 10000)):
            with self.subTest(size=(width, height)), self.assertRaisesRegex(ConversionError, "Canvas"):
                self.plan(background_mode="Solid", canvas_width=width, canvas_height=height)

    def test_random_backgrounds_are_frozen_in_preview_and_match_exports(self):
        self.image("a.png", color=(0, 0, 0, 0))
        self.image("b.png", color=(0, 0, 0, 0))
        options = ConversionOptions(output_format="PNG", background_mode="Solid",
                                    background_distribution="Random solid", random_seed=1234,
                                    canvas_width=30, canvas_height=30)
        plan = build_conversion_plan(self.folder, self.output, options)
        repeated = build_conversion_plan(self.folder, self.output, options)
        self.assertEqual([entry.options for entry in plan.entries],
                         [entry.options for entry in repeated.entries])
        self.assertNotEqual(plan.entries[0].options.background, plan.entries[1].options.background)
        shuffled = build_conversion_plan(self.folder, self.output, replace(options, random_seed=4321))
        self.assertNotEqual(plan.entries[0].options.background, shuffled.entries[0].options.background)
        result = convert_batch(plan)
        self.assertFalse(result.errors)
        for entry in plan.entries:
            with Image.open(entry.target) as output:
                expected = background_layer(output.size, entry.options)
                self.assertEqual(output.tobytes(), expected.tobytes())

    def test_random_gradients_and_mixed_backgrounds_export_selected_styles(self):
        for index in range(6):
            self.image(f"image{index}.png", color=(0, 0, 0, 0))
        for mode in ("Random gradient", "Random mixed", "Random linear gradient", "Random radial gradient"):
            with self.subTest(mode=mode):
                options = ConversionOptions(output_format="PNG", background_mode="Solid",
                                            background_distribution=mode, random_seed=72,
                                            background="#663399", background_end="#ffbb66")
                plan = build_conversion_plan(self.folder, self.root / mode, options)
                styles = {entry.options.background_mode for entry in plan.entries}
                if mode == "Random linear gradient":
                    self.assertEqual(styles, {"Linear gradient"})
                elif mode == "Random radial gradient":
                    self.assertEqual(styles, {"Radial gradient"})
                elif mode == "Random gradient":
                    self.assertNotIn("Solid", styles)
                else:
                    self.assertIn("Solid", styles)
                    self.assertTrue(styles.intersection({"Linear gradient", "Radial gradient"}))
                for entry in plan.entries:
                    if entry.options.background_mode != "Solid":
                        self.assertEqual(entry.options.background, "#663399")
                    self.assertEqual(entry.options.gradient_angle, None)
                    self.assertEqual(entry.options.gradient_center, (0.5, 0.5))
                    self.assertEqual(entry.options.gradient_scale, 1.0)
                self.assertGreater(len({entry.options.background_end for entry in plan.entries}), 1)
                self.assertGreater(len({entry.preview_thumbnail for entry in plan.entries}), 1)
                repeated = build_conversion_plan(self.folder, self.root / mode, options)
                self.assertEqual([entry.options for entry in plan.entries],
                                 [entry.options for entry in repeated.entries])
                recolored = build_conversion_plan(self.folder, self.root / mode,
                                                  replace(options, background="#ff0000", background_end="#0000ff"))
                for before, after in zip(plan.entries, recolored.entries):
                    self.assertEqual(before.options.background_end, after.options.background_end)
                    self.assertEqual(before.options.gradient_angle, after.options.gradient_angle)
                    self.assertEqual(before.options.gradient_center, after.options.gradient_center)
                    self.assertEqual(before.options.gradient_scale, after.options.gradient_scale)
                for entry in plan.entries:
                    with Image.open(io.BytesIO(entry.preview_thumbnail)) as preview:
                        expected = background_layer(preview.size, entry.options)
                        self.assertEqual(preview.tobytes(), expected.tobytes())
                result = convert_batch(plan)
                self.assertFalse(result.errors)
                for entry in plan.entries:
                    with Image.open(entry.target) as output:
                        expected = background_layer(output.size, entry.options)
                        self.assertEqual(output.tobytes(), expected.tobytes())

    def test_live_recolor_keeps_random_outer_colors_and_source_fingerprints(self):
        source = self.image("a.png", color=(0, 0, 0, 0))
        self.image("b.png", color=(0, 0, 0, 0))
        plan = self.plan(output_format="PNG", background_mode="Solid",
                         background_distribution="Random radial gradient", random_seed=99,
                         canvas_width=256, canvas_height=256)
        original_entries = plan.entries
        source.write_bytes(b"changed after preview")
        recolored = recolor_plan(plan, "#ff0000")
        self.assertTrue(recolored.ready)
        for original, changed in zip(original_entries, recolored.entries):
            self.assertEqual(changed.identity, original.identity)
            self.assertEqual(changed.target, original.target)
            self.assertEqual(changed.options.background_end, original.options.background_end)
            self.assertEqual(changed.options.background, "#ff0000")
            with Image.open(io.BytesIO(changed.preview_thumbnail)) as preview:
                center = preview.getpixel((28, 28))
                self.assertGreaterEqual(center[0], 249)
                self.assertLessEqual(center[1], 6)
                self.assertLessEqual(center[2], 6)
        with self.assertRaisesRegex(ConversionError, "changed since preview"):
            convert_batch(recolored)
        self.assertFalse(self.output.exists())

    def test_batch_palette_spreads_colors_without_repeating(self):
        colors = _batch_random_colors(28, 1234)
        self.assertEqual(len(set(colors)), 28)
        self.assertEqual(colors, _batch_random_colors(28, 1234))
        self.assertNotEqual(colors, _batch_random_colors(28, 4321))
        rgb = [ImageColor.getrgb(color) for color in colors]
        for index, color in enumerate(rgb):
            for other in rgb[index + 1:]:
                self.assertGreater(sum((a - b) ** 2 for a, b in zip(color, other)), 60 ** 2)

    def test_large_batch_palette_has_no_duplicates(self):
        self.assertEqual(len(set(_batch_random_colors(1000, 72))), 1000)
        self.assertEqual(_batch_random_colors(0, 72), [])
        with self.assertRaisesRegex(ConversionError, "unique background colors"):
            _batch_random_colors(256 ** 3 + 1, 72)

    def test_distinct_radial_batch_colors_survive_recolor_and_export(self):
        for index in range(28):
            self.image(f"image{index:02}.png", color=(0, 0, 0, 0))
        plan = self.plan(output_format="PNG", background_mode="Solid",
                         background_distribution="Random radial gradient", random_seed=1234,
                         canvas_width=56, canvas_height=56)
        colors = [entry.options.background_end for entry in plan.entries]
        self.assertEqual(colors, _batch_random_colors(28, 1234))
        recolored = recolor_plan(plan, "#ff0000")
        self.assertEqual([entry.options.background_end for entry in recolored.entries], colors)
        self.assertEqual({entry.options.background for entry in recolored.entries}, {"#ff0000"})
        self.assertEqual({entry.options.background_mode for entry in recolored.entries}, {"Radial gradient"})
        result = convert_batch(recolored)
        self.assertFalse(result.errors)
        self.assertEqual(len(result.outputs), 28)
        for entry in recolored.entries:
            with Image.open(entry.target) as output, Image.open(io.BytesIO(entry.preview_thumbnail)) as preview:
                self.assertEqual(output.tobytes(), preview.tobytes())

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
