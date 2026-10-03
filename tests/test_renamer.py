from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.renamer import (
    RenameError, RenameService, build_plan, filename_error, load_names,
    move_no_replace, scan_files,
)


class SimulatedInterruption(BaseException):
    pass


class RenamerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.folder = self.root / "files"
        self.folder.mkdir()
        self.service = RenameService(self.root / "history")

    def file(self, name, content=b"test file"):
        path = self.folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def plan(self, names, **kwargs):
        return build_plan(self.folder, scan_files(self.folder), names, **kwargs)

    def test_load_names_encodings_and_blank_lines(self):
        for encoding in ("utf-8", "utf-8-sig", "utf-16"):
            with self.subTest(encoding=encoding):
                path = self.file("names.txt", "  Beach \n\n\u179a\u17bc\u1794\u1797\u17b6\u1796\n".encode(encoding))
                self.assertEqual(load_names(path), ["Beach", "\u179a\u17bc\u1794\u1797\u17b6\u1796"])

    def test_empty_and_invalid_encoding_rejected(self):
        for content in (b"\n \n", b"\xff\xff\xff"):
            with self.subTest(content=content):
                with self.assertRaises(RenameError):
                    load_names(self.file("names.txt", content))

    def test_scan_natural_order_and_media_filters(self):
        for name in ("IMG_10.JPG", "IMG_2.heic", "VID_3.mp4", "notes.pdf", "song.flac"):
            self.file(name)
        self.assertEqual([p.name for p in scan_files(self.folder)],
                         ["IMG_2.heic", "IMG_10.JPG", "notes.pdf", "song.flac", "VID_3.mp4"])
        self.assertEqual(len(scan_files(self.folder, "Images")), 2)
        self.assertEqual(scan_files(self.folder, "Videos")[0].name, "VID_3.mp4")
        self.assertEqual(scan_files(self.folder, "Audio")[0].name, "song.flac")

    def test_alphabetical_and_modification_time_order(self):
        first = self.file("file2.jpg")
        second = self.file("file10.jpg")
        os.utime(first, (10, 10))
        os.utime(second, (20, 20))
        self.assertEqual(scan_files(self.folder, order="Alphabetical"), [second, first])
        self.assertEqual(scan_files(self.folder, order="Oldest first"), [first, second])
        self.assertEqual(scan_files(self.folder, order="Newest first"), [second, first])

    def test_subfolders_and_exclusion_of_names_file(self):
        self.file("root.mp4")
        self.file("nested/photo.png")
        names = self.file("names.txt")
        self.assertEqual(len(scan_files(self.folder, exclude=names)), 1)
        files = scan_files(self.folder, recursive=True, exclude=names)
        self.assertEqual(len(files), 2)
        plan = build_plan(self.folder, files, ["Picture", "Movie"])
        self.service.apply(plan)
        self.assertTrue((self.folder / "nested/Picture.png").exists())
        self.assertTrue((self.folder / "Movie.mp4").exists())
        self.assertTrue(names.exists())

    def test_preserve_mixed_extensions_and_undo_after_restart(self):
        originals = [("IMG_1.jpg", b"photo"), ("IMG_2.png", b"png"), ("VID_3.mp4", b"video")]
        for name, content in originals:
            self.file(name, content)
        plan = self.plan(["Beach", "Family.png", "Trip"])
        self.assertTrue(plan.ready)
        self.assertEqual(self.service.apply(plan), 3)
        self.assertEqual((self.folder / "Beach.jpg").read_bytes(), b"photo")
        self.assertEqual((self.folder / "Family.png").read_bytes(), b"png")
        self.assertEqual((self.folder / "Trip.mp4").read_bytes(), b"video")
        restarted = RenameService(self.service.history_folder)
        self.assertTrue(restarted.can_undo)
        self.assertEqual(restarted.undo(), 3)
        self.assertFalse(restarted.can_undo)
        for name, content in originals:
            self.assertEqual((self.folder / name).read_bytes(), content)

    def test_complete_filenames_and_extensionless_files(self):
        self.file("a.jpg")
        self.file("b")
        plan = self.plan(["changed.webp", "README"], keep_extensions=False)
        self.service.apply(plan)
        self.assertTrue((self.folder / "changed.webp").exists())
        self.assertTrue((self.folder / "README").exists())

    def test_duplicate_targets_case_insensitive(self):
        self.file("a.jpg")
        self.file("b.jpg")
        plan = self.plan(["New", "new"])
        self.assertFalse(plan.ready)
        self.assertTrue(all("Duplicate" in entry.error for entry in plan.entries))
        with self.assertRaises(RenameError):
            self.service.apply(plan)

    def test_count_mismatch_blocks_all_changes(self):
        self.file("a.jpg")
        self.file("b.jpg")
        for names in (["one"], ["one", "two", "three"]):
            plan = self.plan(names)
            self.assertFalse(plan.ready)
            with self.assertRaises(RenameError):
                self.service.apply(plan)
        self.assertTrue((self.folder / "a.jpg").exists())

    def test_existing_file_and_directory_conflicts(self):
        source = self.file("source.jpg")
        self.file("occupied.jpg", b"keep me")
        (self.folder / "directory.jpg").mkdir()
        for name in ("occupied", "directory"):
            plan = build_plan(self.folder, [source], [name])
            self.assertFalse(plan.ready)
            self.assertIn("exists", plan.entries[0].error)
        self.assertEqual((self.folder / "occupied.jpg").read_bytes(), b"keep me")

    def test_invalid_windows_names_and_paths_blocked(self):
        source = self.file("source.jpg")
        for name in ("../escape", "a/b", "C:\\escape", "bad:name", "CON", "NUL.txt", "LPT1", "end.", "x" * 256):
            with self.subTest(name=name):
                self.assertTrue(filename_error(name))
                plan = build_plan(self.folder, [source], [name])
                self.assertFalse(plan.ready)
                with self.assertRaises(RenameError):
                    self.service.apply(plan)
        self.assertTrue(source.exists())

    def test_swaps_preserve_contents(self):
        self.file("a.jpg", b"A")
        self.file("b.jpg", b"B")
        self.service.apply(self.plan(["b", "a"]))
        self.assertEqual((self.folder / "a.jpg").read_bytes(), b"B")
        self.assertEqual((self.folder / "b.jpg").read_bytes(), b"A")
        self.service.undo()
        self.assertEqual((self.folder / "a.jpg").read_bytes(), b"A")
        self.assertEqual((self.folder / "b.jpg").read_bytes(), b"B")

    def test_case_only_rename_and_unchanged_entry(self):
        self.file("a.jpg")
        self.file("b.jpg")
        plan = self.plan(["A", "b"])
        self.assertEqual(len(plan.changes), 1)
        self.assertEqual(self.service.apply(plan), 1)
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["A.jpg", "b.jpg"])
        self.service.undo()
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["a.jpg", "b.jpg"])

    def test_all_unchanged_is_not_ready(self):
        self.file("a.jpg")
        self.assertFalse(self.plan(["a"]).ready)

    def test_changed_file_after_preview_blocks_rename(self):
        source = self.file("a.jpg")
        plan = self.plan(["new"])
        source.write_bytes(b"modified file")
        with self.assertRaisesRegex(RenameError, "changed"):
            self.service.apply(plan)
        self.assertTrue(source.exists())

    def test_destination_added_after_preview_blocks_rename(self):
        self.file("a.jpg")
        plan = self.plan(["new"])
        existing = self.file("new.jpg", b"do not overwrite")
        with self.assertRaisesRegex(RenameError, "exists"):
            self.service.apply(plan)
        self.assertEqual(existing.read_bytes(), b"do not overwrite")

    def test_undo_refuses_modified_files_and_occupied_originals(self):
        self.file("a.jpg")
        self.service.apply(self.plan(["new"]))
        original = self.file("a.jpg", b"another file")
        with self.assertRaisesRegex(RenameError, "exists"):
            self.service.undo()
        self.assertEqual(original.read_bytes(), b"another file")
        original.unlink()
        (self.folder / "new.jpg").write_bytes(b"edited")
        with self.assertRaisesRegex(RenameError, "changed"):
            self.service.undo()
        self.assertEqual((self.folder / "new.jpg").read_bytes(), b"edited")

    def test_failure_partway_through_commit_restores_names(self):
        self.file("a.jpg", b"A")
        self.file("b.jpg", b"B")
        plan = self.plan(["x", "y"])

        def fail_second_commit(source, target):
            if target.name == "y.jpg":
                raise OSError("simulated permission error")
            move_no_replace(source, target)

        with patch("services.renamer.move_no_replace", side_effect=fail_second_commit):
            with self.assertRaisesRegex(RenameError, "original names were restored"):
                self.service.apply(plan)
        self.assertEqual((self.folder / "a.jpg").read_bytes(), b"A")
        self.assertEqual((self.folder / "b.jpg").read_bytes(), b"B")
        self.assertEqual(len(list(self.folder.iterdir())), 2)
        self.assertFalse(self.service.needs_recovery)

    def test_failure_during_staging_restores_swapped_sources(self):
        self.file("a.jpg", b"A")
        self.file("b.jpg", b"B")

        def fail_second_stage(source, target):
            if source.name == "b.jpg" and target.name.startswith(".smart-rename-"):
                raise OSError("simulated lock on source")
            move_no_replace(source, target)

        with patch("services.renamer.move_no_replace", side_effect=fail_second_stage):
            with self.assertRaisesRegex(RenameError, "original names were restored"):
                self.service.apply(self.plan(["b", "a"]))
        self.assertEqual((self.folder / "a.jpg").read_bytes(), b"A")
        self.assertEqual((self.folder / "b.jpg").read_bytes(), b"B")
        self.assertFalse(self.service.needs_recovery)

    def interrupt_batch(self):
        self.file("a.jpg", b"A")
        self.file("b.jpg", b"B")

        def stop_during_commit(source, target):
            if target.name == "y.jpg":
                raise SimulatedInterruption()
            move_no_replace(source, target)

        with patch("services.renamer.move_no_replace", side_effect=stop_during_commit):
            with self.assertRaises(SimulatedInterruption):
                self.service.apply(self.plan(["x", "y"]))
        return RenameService(self.service.history_folder)

    def test_recover_interrupted_batch_after_restart(self):
        restarted = self.interrupt_batch()
        self.assertTrue(restarted.needs_recovery)
        self.assertFalse(restarted.can_undo)
        self.assertEqual(restarted.recover(), 2)
        self.assertEqual((self.folder / "a.jpg").read_bytes(), b"A")
        self.assertEqual((self.folder / "b.jpg").read_bytes(), b"B")
        self.assertFalse(restarted.needs_recovery)

    def test_recovery_can_itself_be_interrupted_and_retried(self):
        restarted = self.interrupt_batch()

        def stop_during_recovery(source, target):
            if target.name == "b.jpg":
                raise SimulatedInterruption()
            move_no_replace(source, target)

        with patch("services.renamer.move_no_replace", side_effect=stop_during_recovery):
            with self.assertRaises(SimulatedInterruption):
                restarted.recover()
        self.assertEqual(restarted.recover(), 2)
        self.assertEqual((self.folder / "a.jpg").read_bytes(), b"A")
        self.assertEqual((self.folder / "b.jpg").read_bytes(), b"B")

    def test_recovery_does_not_overwrite_new_file(self):
        restarted = self.interrupt_batch()
        occupied = self.file("a.jpg", b"new data")
        with self.assertRaisesRegex(RenameError, "occupied"):
            restarted.recover()
        self.assertEqual(occupied.read_bytes(), b"new data")
        occupied.unlink()
        restarted.recover()
        self.assertEqual(occupied.read_bytes(), b"A")

    def test_previous_undo_history_survives_failed_batch(self):
        self.file("a.jpg", b"A")
        self.service.apply(self.plan(["first"]))
        plan = self.plan(["second"])

        def fail_commit(source, target):
            if target.name == "second.jpg":
                raise OSError("simulated failure")
            move_no_replace(source, target)

        with patch("services.renamer.move_no_replace", side_effect=fail_commit):
            with self.assertRaises(RenameError):
                self.service.apply(plan)
        self.assertTrue(self.service.can_undo)
        self.service.undo()
        self.assertEqual((self.folder / "a.jpg").read_bytes(), b"A")

    def test_interrupted_undo_is_recoverable_then_undo_can_be_retried(self):
        self.file("a.jpg", b"A")
        self.file("b.jpg", b"B")
        self.service.apply(self.plan(["x", "y"]))

        def stop_undo_commit(source, target):
            if target.name == "b.jpg":
                raise SimulatedInterruption()
            move_no_replace(source, target)

        with patch("services.renamer.move_no_replace", side_effect=stop_undo_commit):
            with self.assertRaises(SimulatedInterruption):
                self.service.undo()
        self.service.recover()
        self.assertEqual((self.folder / "x.jpg").read_bytes(), b"A")
        self.assertEqual((self.folder / "y.jpg").read_bytes(), b"B")
        self.assertTrue(self.service.can_undo)
        self.service.undo()
        self.assertEqual((self.folder / "a.jpg").read_bytes(), b"A")
        self.assertEqual((self.folder / "b.jpg").read_bytes(), b"B")

    def test_hard_linked_files_can_recover_without_losing_a_name(self):
        first = self.file("a.jpg", b"shared content")
        second = self.folder / "b.jpg"
        try:
            os.link(first, second)
        except OSError as exc:
            self.skipTest(f"Hard links unavailable: {exc}")

        def stop_before_commit(source, target):
            if target.name == "b.jpg":
                raise SimulatedInterruption()
            move_no_replace(source, target)

        with patch("services.renamer.move_no_replace", side_effect=stop_before_commit):
            with self.assertRaises(SimulatedInterruption):
                self.service.apply(self.plan(["b", "a"]))
        self.service.recover()
        self.assertEqual(first.read_bytes(), b"shared content")
        self.assertEqual(second.read_bytes(), b"shared content")
        self.assertEqual(len(list(self.folder.iterdir())), 2)

    def test_second_instance_cannot_process_a_batch_concurrently(self):
        self.file("a.jpg")
        plan = self.plan(["new"])
        other = RenameService(self.service.history_folder)
        with self.service._lock():
            with self.assertRaisesRegex(RenameError, "Another app instance"):
                other.apply(plan)
        self.assertTrue((self.folder / "a.jpg").exists())


if __name__ == "__main__":
    unittest.main()
