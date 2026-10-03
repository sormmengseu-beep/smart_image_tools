from __future__ import annotations

import colorsys
import io
import math
import os
import random
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from PIL import Image, ImageColor, ImageOps
from pillow_heif import register_heif_opener
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

from services.renamer import Fingerprint, RenameError, fingerprint, move_no_replace, path_key, scan_files


register_heif_opener()
OUTPUT_FORMATS = {"JPG": ("JPEG", ".jpg"), "PNG": ("PNG", ".png"),
                  "WebP": ("WEBP", ".webp"), "BMP": ("BMP", ".bmp"),
                  "TIFF": ("TIFF", ".tiff"), "AVIF": ("AVIF", ".avif")}
INPUT_FORMATS = {"All images": None, "WebP": {".webp", ".webpg"}, "SVG": {".svg"},
                 "JPG": {".jpg", ".jpeg"}, "PNG": {".png"}, "GIF": {".gif"},
                 "HEIC / HEIF": {".heic", ".heif"}, "AVIF": {".avif"},
                 "TIFF": {".tif", ".tiff"}, "BMP": {".bmp"}}
MAX_PIXELS = 40_000_000
BACKGROUND_MODES = ("Original", "Solid", "Linear gradient", "Radial gradient")
GRADIENT_DIRECTIONS = ("Horizontal", "Vertical", "Diagonal")
BACKGROUND_DISTRIBUTIONS = ("Same background", "Random solid", "Random linear gradient",
                            "Random radial gradient", "Random gradient", "Random mixed")
CANVAS_FITS = ("Fit inside", "Fill and crop")


class ConversionError(RenameError):
    pass


@dataclass(frozen=True)
class ConversionOptions:
    output_format: str = "JPG"
    quality: int = 90
    background: str = "#ffffff"
    svg_width: int = 2048
    first_frame: bool = False
    remove_background: bool = False
    background_mode: str = "Original"
    background_end: str = "#8bd3dd"
    gradient_direction: str = "Vertical"
    canvas_width: int = 0
    canvas_height: int = 0
    canvas_fit: str = "Fit inside"
    background_distribution: str = "Same background"
    random_seed: int = 0
    gradient_angle: float | None = None
    gradient_center: tuple[float, float] = (0.5, 0.5)
    gradient_scale: float = 1.0

    @property
    def gradient_description(self) -> str:
        if self.background_mode == "Radial gradient":
            x, y = self.gradient_center
            return f"Center: {x:.0%}, {y:.0%}; spread: {self.gradient_scale:.2f}"
        if self.gradient_angle is not None:
            return f"Angle: {self.gradient_angle:.1f} degrees"
        return self.gradient_direction


@dataclass(frozen=True)
class ConversionEntry:
    source: Path
    target: Path
    identity: Fingerprint
    dimensions: tuple[int, int] = (0, 0)
    thumbnail: bytes = b""
    error: str = ""
    options: ConversionOptions | None = None
    preview_thumbnail: bytes = b""


@dataclass
class ConversionPlan:
    folder: Path
    output_folder: Path
    options: ConversionOptions
    entries: list[ConversionEntry]
    errors: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return bool(self.entries) and not self.errors and not any(e.error for e in self.entries)


@dataclass
class ConversionResult:
    outputs: list[Path] = field(default_factory=list)
    errors: dict[Path, str] = field(default_factory=dict)
    cancelled: bool = False


def _svg_image(data: bytes, options: ConversionOptions) -> Image.Image:
    renderer = QSvgRenderer(QByteArray(data))
    if not renderer.isValid():
        raise ConversionError("Invalid or unsupported SVG")
    if renderer.animated() and not options.first_frame:
        raise ConversionError("Animated SVG: enable First frame")
    if hasattr(renderer, "setAnimationEnabled"):
        renderer.setAnimationEnabled(False)
    size = renderer.defaultSize()
    if size.width() <= 0 or size.height() <= 0:
        raise ConversionError("SVG has no usable dimensions")
    width = options.svg_width
    height = max(1, round(width * size.height() / size.width()))
    if width * height > MAX_PIXELS:
        raise ConversionError("SVG raster size exceeds 40 million pixels")
    raster = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
    if raster.isNull():
        raise ConversionError("Could not allocate SVG image")
    raster.fill(Qt.transparent)
    painter = QPainter(raster)
    try:
        renderer.render(painter)
    finally:
        painter.end()
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    if not raster.save(buffer, "PNG"):
        raise ConversionError("Could not rasterize SVG")
    with Image.open(io.BytesIO(bytes(buffer.data()))) as image:
        return image.copy()


def read_image(path: Path, options: ConversionOptions) -> Image.Image:
    if path.suffix.lower() == ".svg":
        return _svg_image(path.read_bytes(), options)
    with Image.open(path) as image:
        if image.width * image.height > MAX_PIXELS:
            raise ConversionError("Image exceeds 40 million pixels")
        if getattr(image, "n_frames", 1) > 1 and not options.first_frame:
            raise ConversionError("Animated or multipage image: enable First frame")
        image.seek(0)
        return ImageOps.exif_transpose(image)


def _ensure_background_remover() -> None:
    try:
        import rembg  # noqa: F401
    except (ImportError, SystemExit) as exc:
        reason = str(exc) if isinstance(exc, ImportError) else "CPU runtime unavailable"
        command = f'"{sys.executable}" -m pip install "rembg[cpu]>=2.0"'
        if os.name == "nt":
            command = "& " + command
        raise ConversionError(
            f"Background removal could not load rembg ({reason}). "
            f"Install dependencies in the app's Python environment with:\n{command}"
        ) from exc


def _remove_background(image: Image.Image) -> Image.Image:
    _ensure_background_remover()
    from rembg import remove

    result = remove(image.convert("RGBA"))
    if isinstance(result, Image.Image):
        return result.convert("RGBA")
    if isinstance(result, (bytes, bytearray)):
        with Image.open(io.BytesIO(result)) as output:
            return output.convert("RGBA")
    raise ConversionError("Background removal returned an unsupported image")


def background_layer(size: tuple[int, int], options: ConversionOptions) -> Image.Image:
    start = Image.new("RGBA", size, options.background)
    if options.background_mode in {"Original", "Solid"}:
        return start
    end = Image.new("RGBA", size, options.background_end)
    if options.background_mode == "Radial gradient":
        radial = Image.radial_gradient("L")
        if options.gradient_center == (0.5, 0.5) and options.gradient_scale == 1.0:
            mask = radial.resize(size, Image.Resampling.BILINEAR)
        else:
            x_scale = 256 / (size[0] * options.gradient_scale)
            y_scale = 256 / (size[1] * options.gradient_scale)
            x, y = options.gradient_center
            mask = radial.transform(size, Image.Transform.AFFINE,
                                    (x_scale, 0, 128 - x_scale * size[0] * x,
                                     0, y_scale, 128 - y_scale * size[1] * y),
                                    Image.Resampling.BILINEAR, fillcolor=255)
    elif options.gradient_angle is not None:
        width, height = size
        angle = math.radians(options.gradient_angle)
        dx, dy = math.cos(angle), math.sin(angle)
        span = abs(dx) * (width - 1) + abs(dy) * (height - 1)
        if span < 1e-9:
            mask = Image.new("L", size, 0)
        else:
            origin = min(0, dx * (width - 1)) + min(0, dy * (height - 1))
            a, b = 255 * dx / span, 255 * dy / span
            # Map pixel centers onto a normalized ramp across the canvas corners.
            offset = -255 * origin / span + 0.5 - (a + b) / 2
            mask = Image.linear_gradient("L").transform(
                size, Image.Transform.AFFINE, (0, 0, 128, a, b, offset), Image.Resampling.BILINEAR
            )
    else:
        width, height = size
        if options.gradient_direction != "Vertical":
            x = Image.frombytes("L", (width, 1), bytes(
                round(255 * i / max(1, width - 1)) for i in range(width)
            )).resize(size, Image.Resampling.NEAREST)
        if options.gradient_direction != "Horizontal":
            y = Image.frombytes("L", (1, height), bytes(
                round(255 * i / max(1, height - 1)) for i in range(height)
            )).resize(size, Image.Resampling.NEAREST)
        if options.gradient_direction == "Horizontal":
            mask = x
        elif options.gradient_direction == "Vertical":
            mask = y
        else:
            mask = Image.blend(x, y, 0.5)
    return Image.composite(end, start, mask)


def _batch_random_colors(count: int, seed: int) -> list[str]:
    if count > 256 ** 3:
        raise ConversionError("Too many images for unique background colors")
    if not count:
        return []
    rng = random.Random(seed)
    candidates = [tuple(round(channel * 255) for channel in colorsys.hls_to_rgb(
        (hue / 72 + rng.random() / 144) % 1, lightness, saturation
    )) for hue in range(72) for lightness in (0.4, 0.55, 0.7) for saturation in (0.65, 0.85)]
    candidates = list(dict.fromkeys(candidates))
    rng.shuffle(candidates)
    distances = [float("inf")] * len(candidates)
    selected = []
    # Farthest-first selection spreads small batches across the whole palette.
    while candidates and len(selected) < count:
        index = max(range(len(candidates)), key=distances.__getitem__)
        color = candidates.pop(index)
        distances.pop(index)
        selected.append(color)
        distances = [min(distance, sum((a - b) ** 2 for a, b in zip(candidate, color)))
                     for candidate, distance in zip(candidates, distances)]
    used = set(selected)
    while len(selected) < count:
        color = tuple(rng.randrange(256) for _ in range(3))
        if color not in used:
            selected.append(color)
            used.add(color)
    return ["#" + "".join(f"{channel:02x}" for channel in color) for color in selected]


def options_for_image(options: ConversionOptions, image_key: str,
                      random_color: str | None = None) -> ConversionOptions:
    if options.background_distribution == "Same background":
        return options
    rng = random.Random(f"{options.random_seed}:{image_key}")
    mode = options.background_distribution
    if mode == "Random mixed":
        mode = rng.choice(("Random solid", "Random gradient"))
    if mode == "Random solid":
        style = "Solid"
    elif mode == "Random linear gradient":
        style = "Linear gradient"
    elif mode == "Random radial gradient":
        style = "Radial gradient"
    else:
        style = rng.choice(("Linear gradient", "Radial gradient"))
    if random_color is None:
        hue = rng.random()
        rgb = colorsys.hls_to_rgb(hue, rng.uniform(0.5, 0.8), rng.uniform(0.5, 0.85))
        random_color = "#" + "".join(f"{round(channel * 255):02x}" for channel in rgb)
    return replace(options, background_mode=style,
                   background=random_color if style == "Solid" else options.background,
                   background_end=random_color,
                   background_distribution="Same background")


def apply_background(image: Image.Image, options: ConversionOptions) -> Image.Image:
    size = (options.canvas_width, options.canvas_height) if options.canvas_width else image.size
    subject = image.convert("RGBA")
    if size != image.size:
        if options.canvas_fit == "Fill and crop":
            subject = ImageOps.fit(subject, size, Image.Resampling.LANCZOS)
        else:
            subject = ImageOps.contain(subject, size, Image.Resampling.LANCZOS)
    foreground = Image.new("RGBA", size)
    foreground.paste(subject, ((size[0] - subject.width) // 2, (size[1] - subject.height) // 2))
    return Image.alpha_composite(background_layer(size, options), foreground)


def _transparent_output(options: ConversionOptions) -> bool:
    return options.remove_background and options.background_mode == "Original"


def render_preview(image: Image.Image, options: ConversionOptions,
                   bounds: tuple[int, int] = (56, 56)) -> Image.Image:
    if options.background_mode != "Original":
        size = (options.canvas_width, options.canvas_height) if options.canvas_width else image.size
        scale = min(bounds[0] / size[0], bounds[1] / size[1])
        preview_size = (max(1, round(size[0] * scale)), max(1, round(size[1] * scale)))
        return apply_background(image, replace(options, canvas_width=preview_size[0],
                                               canvas_height=preview_size[1]))
    preview = image.convert("RGBA")
    preview.thumbnail(bounds, Image.Resampling.LANCZOS)
    if not _transparent_output(options) and options.output_format in {"JPG", "BMP"}:
        preview = apply_background(preview, replace(options, canvas_width=0, canvas_height=0))
    return preview


def recolor_plan(plan: ConversionPlan, background: str) -> ConversionPlan:
    options = replace(plan.options, background=background)
    entries = []
    for entry in plan.entries:
        entry_options = entry.options or options_for_image(
            options, entry.source.relative_to(plan.folder).as_posix())
        if entry_options.background_mode != "Solid" or options.background_distribution == "Same background":
            entry_options = replace(entry_options, background=background)
        thumbnail = entry.preview_thumbnail
        if entry.thumbnail:
            preview_options = entry_options
            if not entry_options.canvas_width and entry_options.background_mode != "Original":
                preview_options = replace(entry_options, canvas_width=entry.dimensions[0],
                                          canvas_height=entry.dimensions[1])
            with Image.open(io.BytesIO(entry.thumbnail)) as image:
                with render_preview(image, preview_options) as preview:
                    buffer = io.BytesIO()
                    preview.save(buffer, format="PNG")
                    thumbnail = buffer.getvalue()
        entries.append(replace(entry, options=entry_options, preview_thumbnail=thumbnail))
    return replace(plan, options=options, entries=entries)


def build_conversion_plan(folder: Path, output_folder: Path, options: ConversionOptions,
                          input_format: str = "All images", recursive: bool = False,
                          progress: Callable[[str], None] = lambda message: None) -> ConversionPlan:
    folder = folder.resolve(strict=True)
    output_folder = output_folder.resolve()
    if not _transparent_output(options) and options.output_format not in OUTPUT_FORMATS:
        raise ConversionError("Choose a supported output format")
    if not 1 <= options.quality <= 100 or not 128 <= options.svg_width <= 8192:
        raise ConversionError("Invalid quality or SVG width")
    if options.background_mode not in BACKGROUND_MODES:
        raise ConversionError("Choose a supported background style")
    if options.gradient_direction not in GRADIENT_DIRECTIONS:
        raise ConversionError("Choose a supported gradient direction")
    if (options.gradient_angle is not None and not math.isfinite(options.gradient_angle)
            or len(options.gradient_center) != 2
            or any(not 0 <= value <= 1 for value in options.gradient_center)
            or not 0.1 <= options.gradient_scale <= 3):
        raise ConversionError("Invalid gradient angle, center, or spread")
    if options.canvas_fit not in CANVAS_FITS:
        raise ConversionError("Choose a supported image fit")
    if options.background_distribution not in BACKGROUND_DISTRIBUTIONS:
        raise ConversionError("Choose a supported batch background mode")
    width, height = options.canvas_width, options.canvas_height
    if (width, height) != (0, 0) and (
            not 1 <= width <= 20000 or not 1 <= height <= 20000 or width * height > MAX_PIXELS):
        raise ConversionError("Canvas dimensions must be 1 to 20000 pixels and at most 40 million pixels")
    if options.background_distribution != "Same background" and options.background_mode == "Original":
        raise ConversionError("Random backgrounds require Add background")
    try:
        for color in (options.background, options.background_end):
            if len(ImageColor.getrgb(color)) != 3:
                raise ValueError("Background colors must be opaque")
    except (ValueError, TypeError) as exc:
        raise ConversionError("Choose valid, opaque background colors") from exc
    if options.remove_background:
        _ensure_background_remover()
    if folder == output_folder:
        raise ConversionError("Choose a separate output folder to keep the originals intact")
    if output_folder.exists() and not output_folder.is_dir():
        raise ConversionError("Output folder is a file")
    files = scan_files(folder, "Images", recursive, exclude_folders=(output_folder,))
    extensions = INPUT_FORMATS[input_format]
    if extensions:
        files = [path for path in files if path.suffix.lower() in extensions]
    suffix = ".png" if _transparent_output(options) else OUTPUT_FORMATS[options.output_format][1]
    targets = [output_folder / source.relative_to(folder).with_suffix(suffix) for source in files]
    counts = Counter(path_key(path) for path in targets)
    occupied = set()
    for parent in {path.parent for path in targets}:
        if parent.exists():
            if not parent.is_dir():
                raise ConversionError(f"Output subfolder is a file: {parent}")
            occupied.update(path_key(path) for path in parent.iterdir())
    entries = []
    colors = (_batch_random_colors(len(files), options.random_seed)
              if options.background_distribution != "Same background" else [None] * len(files))
    for index, (source, target) in enumerate(zip(files, targets), 1):
        entry_options = options_for_image(options, source.relative_to(folder).as_posix(), colors[index - 1])
        progress(f"Checking image {index}/{len(files)}: {source.name}")
        identity = fingerprint(source)
        dimensions = (0, 0)
        thumbnail = b""
        preview_thumbnail = b""
        error = ""
        if counts[path_key(target)] > 1:
            error = "Duplicate output name"
        elif path_key(target) in occupied:
            error = "Output already exists"
        else:
            try:
                with read_image(source, options) as image:
                    dimensions = image.size
                    with render_preview(image, entry_options) as preview:
                        buffer = io.BytesIO()
                        preview.save(buffer, format="PNG")
                        preview_thumbnail = buffer.getvalue()
                    image.thumbnail((56, 56))
                    buffer = io.BytesIO()
                    image.convert("RGBA").save(buffer, format="PNG")
                    thumbnail = buffer.getvalue()
                if fingerprint(source) != identity:
                    error = "Source changed while reading"
            except Exception as exc:
                error = str(exc) or "Unsupported or corrupt image"
        entries.append(ConversionEntry(source, target, identity, dimensions, thumbnail, error,
                                       entry_options, preview_thumbnail))
    errors = [] if files else ["No images match the selection"]
    return ConversionPlan(folder, output_folder, options, entries, errors)


def _save_image(image: Image.Image, path: Path, options: ConversionOptions) -> None:
    output = _remove_background(image) if options.remove_background else image.convert("RGBA")
    if options.background_mode != "Original":
        output = apply_background(output, options)
    image_format = "PNG" if _transparent_output(options) else OUTPUT_FORMATS[options.output_format][0]
    parameters = {}
    if image_format in {"JPEG", "WEBP", "AVIF"}:
        parameters["quality"] = options.quality
    if image_format == "JPEG":
        parameters.update(optimize=True, subsampling=0)
    if image_format in {"JPEG", "PNG", "WEBP", "TIFF", "AVIF"}:
        exif = image.getexif()
        if exif:
            if output.size != image.size:
                for tag, value in ((256, output.width), (257, output.height)):
                    if tag in exif:
                        exif[tag] = value
                if 34665 in exif:
                    dimensions = exif.get_ifd(34665)
                    for tag, value in ((40962, output.width), (40963, output.height)):
                        if tag in dimensions:
                            dimensions[tag] = value
            parameters["exif"] = exif.tobytes()
        if image.info.get("icc_profile"):
            parameters["icc_profile"] = image.info["icc_profile"]
    if image_format in {"JPEG", "BMP"}:
        flattened = Image.new("RGB", output.size, options.background)
        flattened.paste(output, mask=output.getchannel("A"))
        flattened.save(path, format=image_format, **parameters)
    else:
        output.save(path, format=image_format, **parameters)


def convert_batch(plan: ConversionPlan, progress: Callable[[str], None] = lambda message: None,
                  cancelled: Callable[[], bool] = lambda: False) -> ConversionResult:
    if not plan.ready:
        raise ConversionError("Resolve all preview errors before converting")
    for entry in plan.entries:
        if entry.source.is_symlink() or fingerprint(entry.source) != entry.identity:
            raise ConversionError(f"Source changed since preview: {entry.source.name}. Preview again.")
    result = ConversionResult()
    for index, entry in enumerate(plan.entries, 1):
        if cancelled():
            result.cancelled = True
            break
        action = ("Adding background to" if plan.options.background_mode != "Original" else
                  "Removing background from" if plan.options.remove_background else "Converting")
        progress(f"{action} {index}/{len(plan.entries)}: {entry.source.name}")
        temporary = None
        try:
            if fingerprint(entry.source) != entry.identity:
                raise ConversionError("Source changed since preview")
            if entry.target.parent.resolve() != entry.target.parent:
                raise ConversionError("Output folder changed since preview")
            entry.target.parent.mkdir(parents=True, exist_ok=True)
            if any(path_key(path) == path_key(entry.target) for path in entry.target.parent.iterdir()):
                raise ConversionError("Output already exists")
            descriptor, name = tempfile.mkstemp(prefix=".smart-convert-", suffix=".tmp", dir=entry.target.parent)
            os.close(descriptor)
            temporary = Path(name)
            with read_image(entry.source, plan.options) as image:
                _save_image(image, temporary, entry.options or plan.options)
            if fingerprint(entry.source) != entry.identity:
                raise ConversionError("Source changed during conversion")
            # Preserve modification time so chronological ordering survives conversion.
            os.utime(temporary, ns=(entry.identity[3], entry.identity[3]))
            move_no_replace(temporary, entry.target)
            result.outputs.append(entry.target)
        except Exception as exc:
            result.errors[entry.source] = str(exc)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()
    return result
