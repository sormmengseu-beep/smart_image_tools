from __future__ import annotations

import io
import os
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from PIL import Image, ImageOps
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


@dataclass(frozen=True)
class ConversionEntry:
    source: Path
    target: Path
    identity: Fingerprint
    dimensions: tuple[int, int] = (0, 0)
    thumbnail: bytes = b""
    error: str = ""


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


def build_conversion_plan(folder: Path, output_folder: Path, options: ConversionOptions,
                          input_format: str = "All images", recursive: bool = False,
                          progress: Callable[[str], None] = lambda message: None) -> ConversionPlan:
    folder = folder.resolve(strict=True)
    output_folder = output_folder.resolve()
    if not options.remove_background and options.output_format not in OUTPUT_FORMATS:
        raise ConversionError("Choose a supported output format")
    if not 1 <= options.quality <= 100 or not 128 <= options.svg_width <= 8192:
        raise ConversionError("Invalid quality or SVG width")
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
    suffix = ".png" if options.remove_background else OUTPUT_FORMATS[options.output_format][1]
    targets = [output_folder / source.relative_to(folder).with_suffix(suffix) for source in files]
    counts = Counter(path_key(path) for path in targets)
    occupied = set()
    for parent in {path.parent for path in targets}:
        if parent.exists():
            if not parent.is_dir():
                raise ConversionError(f"Output subfolder is a file: {parent}")
            occupied.update(path_key(path) for path in parent.iterdir())
    entries = []
    for index, (source, target) in enumerate(zip(files, targets), 1):
        progress(f"Checking image {index}/{len(files)}: {source.name}")
        identity = fingerprint(source)
        dimensions = (0, 0)
        thumbnail = b""
        error = ""
        if counts[path_key(target)] > 1:
            error = "Duplicate output name"
        elif path_key(target) in occupied:
            error = "Output already exists"
        else:
            try:
                with read_image(source, options) as image:
                    dimensions = image.size
                    image.thumbnail((56, 56))
                    buffer = io.BytesIO()
                    image.convert("RGBA").save(buffer, format="PNG")
                    thumbnail = buffer.getvalue()
                if fingerprint(source) != identity:
                    error = "Source changed while reading"
            except Exception as exc:
                error = str(exc) or "Unsupported or corrupt image"
        entries.append(ConversionEntry(source, target, identity, dimensions, thumbnail, error))
    errors = [] if files else ["No images match the selection"]
    return ConversionPlan(folder, output_folder, options, entries, errors)


def _save_image(image: Image.Image, path: Path, options: ConversionOptions) -> None:
    if options.remove_background:
        output = _remove_background(image)
        parameters = {}
        if image.info.get("icc_profile"):
            parameters["icc_profile"] = image.info["icc_profile"]
        output.save(path, format="PNG", **parameters)
        return
    image_format = OUTPUT_FORMATS[options.output_format][0]
    parameters = {}
    if image_format in {"JPEG", "WEBP", "AVIF"}:
        parameters["quality"] = options.quality
    if image_format == "JPEG":
        parameters.update(optimize=True, subsampling=0)
    if image_format in {"JPEG", "PNG", "WEBP", "TIFF", "AVIF"}:
        exif = image.getexif()
        if exif:
            parameters["exif"] = exif.tobytes()
        if image.info.get("icc_profile"):
            parameters["icc_profile"] = image.info["icc_profile"]
    if image_format in {"JPEG", "BMP"}:
        rgba = image.convert("RGBA")
        flattened = Image.new("RGB", image.size, options.background)
        flattened.paste(rgba, mask=rgba.getchannel("A"))
        flattened.save(path, format=image_format, **parameters)
    else:
        image.convert("RGBA").save(path, format=image_format, **parameters)


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
        action = "Removing background from" if plan.options.remove_background else "Converting"
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
                _save_image(image, temporary, plan.options)
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
