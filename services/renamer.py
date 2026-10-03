from __future__ import annotations

import json
import csv
import os
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable


IMAGE_EXTENSIONS = frozenset(
    ".jpg .jpeg .png .gif .webp .webpg .bmp .tif .tiff .svg .ico .heic .heif .avif "
    ".raw .cr2 .cr3 .nef .arw .dng .psd .exr .jxl .jfif .jpe .jp2 .j2k .apng "
    ".tga .pcx .ppm .pgm .pbm .pnm .dds .dib .qoi .icns .sgi .rgb .rgba .xbm .xpm .hif".split()
)
VIDEO_EXTENSIONS = frozenset(
    ".mp4 .mov .mkv .avi .webm .m4v .wmv .flv .mpeg .mpg .3gp .ts .mts .m2ts .vob .ogv".split()
)
AUDIO_EXTENSIONS = frozenset(
    ".mp3 .wav .flac .aac .m4a .ogg .opus .wma .aiff .alac .mid .midi".split()
)
FILE_TYPES = {"All files": None, "Images": IMAGE_EXTENSIONS,
              "Videos": VIDEO_EXTENSIONS, "Audio": AUDIO_EXTENSIONS}
Progress = Callable[[str], None]
Fingerprint = tuple[int, int, int, int]


class RenameError(ValueError):
    pass


def fingerprint(path: Path) -> Fingerprint:
    info = path.stat(follow_symlinks=False)
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def path_key(path: Path) -> str:
    return str(path.absolute()).casefold()


def natural_key(value: str) -> tuple:
    return tuple((1, int(part)) if part.isdigit() else (0, part.casefold())
                 for part in re.split(r"(\d+)", value))


def file_time_ns(path: Path, date_field: str = "Modified") -> int:
    info = path.stat()
    if date_field == "Created":
        if hasattr(info, "st_birthtime_ns"):
            return info.st_birthtime_ns
        if os.name == "nt":
            return info.st_ctime_ns
        raise RenameError("Created date is unavailable on this filesystem. Choose Modified date.")
    return info.st_mtime_ns


def display_time(value: int) -> str:
    return datetime.fromtimestamp(value / 1_000_000_000).strftime("%Y-%m-%d %H:%M:%S.%f")


def load_names(path: Path) -> list[str]:
    raw = path.read_bytes()
    encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    try:
        content = raw.decode(encoding)
    except UnicodeDecodeError as exc:
        raise RenameError("The names file must use UTF-8 or UTF-16 text encoding.") from exc
    names = [line.strip() for line in content.splitlines() if line.strip()]
    if not names:
        raise RenameError("The TXT file contains no names.")
    return names


def scan_files(folder: Path, file_type: str = "All files", recursive: bool = False,
               order: str = "Natural name", exclude: Path | None = None,
               date_field: str = "Modified", exclude_folders: tuple[Path, ...] = ()) -> list[Path]:
    folder = folder.resolve(strict=True)
    if not folder.is_dir():
        raise RenameError("Select an existing folder.")
    extensions = FILE_TYPES[file_type]
    files = []
    excluded = path_key(exclude.resolve()) if exclude else None
    excluded_folders = {path_key(path.resolve()) for path in exclude_folders}

    def visit(directory: Path) -> None:
        with os.scandir(directory) as children:
            for item in children:
                path = Path(item.path)
                if item.is_symlink():
                    continue
                if hasattr(path, "is_junction") and path.is_junction():
                    continue
                if item.is_dir(follow_symlinks=False):
                    if recursive and path_key(path) not in excluded_folders:
                        visit(path)
                elif item.is_file(follow_symlinks=False):
                    if path_key(path) == excluded:
                        continue
                    if extensions is None or path.suffix.lower() in extensions:
                        files.append(path)

    visit(folder)
    files.sort(key=lambda path: (natural_key(str(path.relative_to(folder))), str(path)))
    if order == "Alphabetical":
        files.sort(key=lambda path: str(path.relative_to(folder)).casefold())
    elif order in {"Oldest first", "Newest first"}:
        files.sort(key=lambda path: file_time_ns(path, date_field), reverse=order == "Newest first")
    return files


def filename_error(name: str) -> str:
    if not name or name in {".", ".."}:
        return "Empty or invalid filename"
    if re.search(r'[<>:"/\\|?*\x00-\x1f]', name):
        return 'Filename contains a forbidden character: < > : " / \\ | ? *'
    if name.endswith((".", " ")):
        return "Filename cannot end with a dot or space"
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    reserved.update(f"{prefix}{number}" for prefix in ("COM", "LPT")
                    for number in list("123456789") + ["\u00b9", "\u00b2", "\u00b3"])
    if name.split(".", 1)[0].rstrip().upper() in reserved:
        return "Filename is reserved by Windows"
    if len(name.encode("utf-16-le")) // 2 > 255:
        return "Filename exceeds 255 characters"
    return ""


@dataclass(frozen=True)
class RenameEntry:
    source: Path
    target: Path
    identity: Fingerprint
    error: str = ""
    timestamp_ns: int = 0

    @property
    def changed(self) -> bool:
        return self.source.name != self.target.name


@dataclass
class RenamePlan:
    folder: Path
    entries: list[RenameEntry]
    names_count: int
    errors: list[str] = field(default_factory=list)
    date_field: str = "Modified"

    @property
    def changes(self) -> list[RenameEntry]:
        return [entry for entry in self.entries if entry.changed]

    @property
    def ready(self) -> bool:
        return bool(self.changes) and not self.errors and not any(e.error for e in self.entries)


def build_plan(folder: Path, files: list[Path], names: list[str],
               keep_extensions: bool = True, date_field: str = "Modified") -> RenamePlan:
    errors = []
    if len(names) != len(files):
        errors.append(f"{len(files)} files and {len(names)} names. Counts must match.")
    if not files:
        errors.append("No files match the current selection.")
    targets: list[Path] = []
    for index, source in enumerate(files):
        name = names[index] if index < len(names) else source.name
        if keep_extensions and source.suffix and not name.lower().endswith(source.suffix.lower()):
            name += source.suffix
        elif keep_extensions and source.suffix:
            name = name[:-len(source.suffix)] + source.suffix
        targets.append(source.with_name(name) if not filename_error(name) else source.parent / name)

    target_counts: dict[str, int] = {}
    for target in targets:
        key = path_key(target)
        target_counts[key] = target_counts.get(key, 0) + 1
    sources = {path_key(path) for path in files}
    occupied = set()
    for parent in {path.parent for path in files}:
        occupied.update(path_key(path) for path in parent.iterdir())
    entries = []
    for index, (source, target) in enumerate(zip(files, targets)):
        name = names[index] if index < len(names) else ""
        error = filename_error(name) if index < len(names) else "Missing name"
        if not error:
            error = filename_error(target.name)
        if not error and target_counts[path_key(target)] > 1:
            error = "Duplicate destination name"
        if not error and path_key(target) in occupied and path_key(target) not in sources:
            error = "Destination already exists"
        entries.append(RenameEntry(source, target, fingerprint(source), error, file_time_ns(source, date_field)))
    return RenamePlan(folder.resolve(), entries, len(names), errors, date_field)


def build_mapped_plan(folder: Path, files: list[Path], manifest: Path,
                      keep_extensions: bool = True, date_field: str = "Modified") -> RenamePlan:
    with manifest.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not {"current_file", "new_name"}.issubset(reader.fieldnames):
            raise RenameError("CSV must contain current_file and new_name columns.")
        records = list(reader)
    mappings = {}
    for record in records:
        relative = (record.get("current_file") or "").strip().replace("\\", "/")
        path = Path(relative)
        if not relative or path.is_absolute() or ".." in path.parts or ":" in relative:
            raise RenameError(f"CSV contains an invalid relative source path: {relative}")
        key = path_key(folder.resolve() / path)
        if key in mappings:
            raise RenameError(f"CSV lists the same file twice: {relative}")
        mappings[key] = record
    names = [(mappings.get(path_key(path), {}).get("new_name") or "").strip() for path in files]
    plan = build_plan(folder, files, names, keep_extensions, date_field)
    plan.names_count = len(records)
    selected = {path_key(path) for path in files}
    if set(mappings) != selected:
        plan.errors.append("CSV source files do not match the selected files. Check the folder and file filter.")
    for entry in plan.entries:
        record = mappings.get(path_key(entry.source), {})
        snapshot = (record.get("identity") or "").strip()
        if snapshot:
            try:
                expected = tuple(json.loads(snapshot))
            except (TypeError, ValueError) as exc:
                raise RenameError(f"Invalid identity in CSV for {entry.source.name}") from exc
            if expected != entry.identity:
                plan.errors.append(f"File changed since CSV export: {entry.source.name}")
    return plan


def export_mapping(plan: RenamePlan, path: Path, keep_extensions: bool = True) -> None:
    if path_key(path.resolve()) in {path_key(entry.source) for entry in plan.entries}:
        raise RenameError("The mapping must not replace a selected source file.")
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["current_file", "new_name", "modified_time", "identity"])
        writer.writeheader()
        for entry in plan.entries:
            target = entry.target if not entry.error else entry.source
            writer.writerow({
                "current_file": str(entry.source.relative_to(plan.folder)),
                "new_name": target.stem if keep_extensions and entry.source.suffix else target.name,
                "modified_time": display_time(entry.identity[3]),
                "identity": json.dumps(entry.identity),
            })


def move_no_replace(source: Path, target: Path) -> None:
    if os.name == "nt":
        os.rename(source, target)
    else:
        # Link creation fails atomically if another file already owns the name.
        os.link(source, target, follow_symlinks=False)
        try:
            source.unlink()
        except OSError:
            target.unlink()
            raise


class RenameService:
    def __init__(self, history_folder: Path) -> None:
        self.history_folder = history_folder
        self.history_path = history_folder / "last_rename.json"
        self.pending_path = history_folder / "pending_rename.json"

    @property
    def can_undo(self) -> bool:
        return self.history_path.exists() and not self.needs_recovery

    @property
    def needs_recovery(self) -> bool:
        return self.pending_path.exists()

    def _read(self, path: Path) -> dict:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RenameError(f"Cannot read rename history: {path}") from exc

    def _write(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    @contextmanager
    def _lock(self):
        self.history_folder.mkdir(parents=True, exist_ok=True)
        with (self.history_folder / "batch.lock").open("a+b") as handle:
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RenameError("Another app instance is processing a batch. Try again when it finishes.") from exc
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _validate(self, entries: list[RenameEntry]) -> None:
        if len({path_key(e.source) for e in entries}) != len(entries):
            raise RenameError("Duplicate source paths in rename batch.")
        if len({path_key(e.target) for e in entries}) != len(entries):
            raise RenameError("Duplicate destination paths in rename batch.")
        sources = {path_key(entry.source) for entry in entries}
        occupied = set()
        for parent in {entry.source.parent for entry in entries}:
            occupied.update(path_key(path) for path in parent.iterdir())
        for entry in entries:
            if entry.error or filename_error(entry.target.name):
                raise RenameError(entry.error or filename_error(entry.target.name))
            if entry.source.parent != entry.target.parent:
                raise RenameError("Renaming cannot move a file to a different folder.")
            if entry.source.is_symlink() or not entry.source.is_file():
                raise RenameError(f"Source file is missing or changed: {entry.source}")
            if fingerprint(entry.source) != entry.identity:
                raise RenameError(f"File changed since preview: {entry.source.name}. Refresh the preview.")
            if path_key(entry.target) in occupied and path_key(entry.target) not in sources:
                raise RenameError(f"Destination already exists: {entry.target}")

    def apply(self, plan: RenamePlan, progress: Progress = lambda message: None) -> int:
        if not plan.ready:
            raise RenameError("Resolve all preview errors before renaming.")
        with self._lock():
            return self._execute(plan.changes, "rename", progress)

    def undo(self, progress: Progress = lambda message: None) -> int:
        with self._lock():
            if not self.can_undo:
                raise RenameError("No completed batch is available to undo.")
            data = self._read(self.history_path)
            entries = [RenameEntry(Path(item["target"]), Path(item["source"]), tuple(item["identity"]))
                       for item in data["entries"]]
            return self._execute(entries, "undo", progress)

    def _execute(self, entries: list[RenameEntry], action: str, progress: Progress) -> int:
        if self.needs_recovery:
            raise RenameError("Recover the interrupted batch before renaming more files.")
        self._validate(entries)
        previous = self._read(self.history_path) if self.history_path.exists() else None
        records = [{"source": str(e.source), "target": str(e.target), "identity": e.identity,
                    "temporary": str(e.source.parent / f".smart-rename-{uuid.uuid4().hex}.tmp")}
                   for e in entries]
        pending = {"action": action, "phase": "moving", "entries": records, "previous": previous}
        self._write(self.pending_path, pending)
        try:
            # Stage the whole batch first, allowing swaps and case-only renames.
            for index, (entry, record) in enumerate(zip(entries, records), 1):
                if fingerprint(entry.source) != entry.identity:
                    raise RenameError(f"File changed during rename: {entry.source}")
                move_no_replace(entry.source, Path(record["temporary"]))
                progress(f"Preparing {index}/{len(entries)}")
            for index, record in enumerate(records, 1):
                move_no_replace(Path(record["temporary"]), Path(record["target"]))
                progress(f"Renaming {index}/{len(entries)}")
            if action == "rename":
                self._write(self.history_path, {"entries": records})
            else:
                self.history_path.unlink(missing_ok=True)
            pending["phase"] = "complete"
            self._write(self.pending_path, pending)
        except Exception as exc:
            try:
                self._recover(progress)
            except Exception as recovery_error:
                raise RenameError(f"Rename interrupted: {exc}\nRecovery needed: {recovery_error}") from exc
            raise RenameError(f"Rename failed; original names were restored. {exc}") from exc
        self.pending_path.unlink()
        return len(entries)

    def recover(self, progress: Progress = lambda message: None) -> int:
        with self._lock():
            return self._recover(progress)

    def _recover(self, progress: Progress) -> int:
        if not self.needs_recovery:
            return 0
        data = self._read(self.pending_path)
        records = data["entries"]
        if data["phase"] == "complete":
            self.pending_path.unlink()
            return 0
        candidates_by_record = []
        for record in records:
            identity = tuple(record["identity"])
            candidates = [Path(record[key]) for key in ("recovery", "temporary", "source", "target")
                          if key in record]
            matches = [path for path in candidates if path.is_file() and not path.is_symlink()
                       and fingerprint(path) == identity]
            if not matches:
                raise RenameError(f"Cannot locate the unchanged file originally named {record['source']}.")
            candidates_by_record.append(matches)
        # Hard-linked sources can share identities; assign each existing path once.
        owners: dict[str, int] = {}
        locations: dict[int, Path] = {}

        def assign(index: int, visited: set[str]) -> bool:
            for path in candidates_by_record[index]:
                key = path_key(path)
                if key in visited:
                    continue
                visited.add(key)
                if key not in owners or assign(owners[key], visited):
                    owners[key] = index
                    locations[index] = path
                    return True
            return False

        for index in range(len(records)):
            if not assign(index, set()):
                raise RenameError("Cannot uniquely locate all original files for recovery.")
        occupied = set()
        for parent in {Path(record["source"]).parent for record in records}:
            occupied.update(path_key(path) for path in parent.iterdir())
        for record in records:
            original_key = path_key(Path(record["source"]))
            if original_key in occupied and original_key not in owners:
                raise RenameError(f"Original name is occupied: {record['source']}. Recovery cannot overwrite it.")
        # Persist recovery names too, so another interruption remains recoverable.
        recovery_paths = []
        for record in records:
            if "recovery" not in record:
                record["recovery"] = str(Path(record["source"]).parent / f".smart-restore-{uuid.uuid4().hex}.tmp")
            recovery_paths.append(Path(record["recovery"]))
        self._write(self.pending_path, data)
        for index, temporary in enumerate(recovery_paths):
            location = locations[index]
            if location != temporary:
                move_no_replace(location, temporary)
        for index, (record, temporary) in enumerate(zip(records, recovery_paths), 1):
            move_no_replace(temporary, Path(record["source"]))
            progress(f"Restoring {index}/{len(records)}")
        if data["previous"] is None:
            self.history_path.unlink(missing_ok=True)
        else:
            self._write(self.history_path, data["previous"])
        self.pending_path.unlink()
        return len(records)
