from __future__ import annotations

import csv
import os
import tempfile
import unittest
from pathlib import Path

from services.renamer import (
    RenameError, RenameService, build_mapped_plan, build_plan,
    export_mapping, file_time_ns, scan_files,
)


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.folder = self.root / "files"
        self.folder.mkdir()
        self.a = self.folder / "a.jpg"
        self.b = self.folder / "b.jpg"
        self.a.write_bytes(b"A")
        self.b.write_bytes(b"B")
        self.manifest = self.root / "mapping.csv"

    def write_rows(self, rows):
        with self.manifest.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def test_mapping_does_not_depend_on_file_order(self):
        self.write_rows([{"current_file": "a.jpg", "new_name": "Portrait"},
                         {"current_file": "b.jpg", "new_name": "Landscape"}])
        plan = build_mapped_plan(self.folder, [self.b, self.a], self.manifest)
        self.assertEqual([entry.target.name for entry in plan.entries], ["Landscape.jpg", "Portrait.jpg"])
        RenameService(self.root / "history").apply(plan)
        self.assertEqual((self.folder / "Portrait.jpg").read_bytes(), b"A")
        self.assertEqual((self.folder / "Landscape.jpg").read_bytes(), b"B")

    def test_exported_snapshot_blocks_replaced_files(self):
        plan = build_plan(self.folder, [self.a, self.b], ["Portrait", "Landscape"])
        export_mapping(plan, self.manifest)
        self.a.write_bytes(b"different contents")
        imported = build_mapped_plan(self.folder, [self.a, self.b], self.manifest)
        self.assertFalse(imported.ready)
        self.assertIn("File changed since CSV export", imported.errors[0])

    def test_export_import_preserves_exact_pairs_and_subfolder_paths(self):
        nested = self.folder / "nested/comma,photo.jpg"
        nested.parent.mkdir()
        nested.write_bytes(b"nested")
        plan = build_plan(self.folder, [self.a, nested], ["Name, with comma", "Photo"])
        export_mapping(plan, self.manifest)
        imported = build_mapped_plan(self.folder, [nested, self.a], self.manifest)
        self.assertTrue(imported.ready)
        self.assertEqual(imported.entries[0].target.name, "Photo.jpg")
        self.assertEqual(imported.entries[1].target.name, "Name, with comma.jpg")

    def test_missing_extra_duplicate_and_traversal_sources_blocked(self):
        for rows in (
            [{"current_file": "a.jpg", "new_name": "One"}],
            [{"current_file": "missing.jpg", "new_name": "One"}, {"current_file": "a.jpg", "new_name": "Two"}],
        ):
            self.write_rows(rows)
            plan = build_mapped_plan(self.folder, [self.a, self.b], self.manifest)
            self.assertFalse(plan.ready)
        for rows in (
            [{"current_file": "a.jpg", "new_name": "One"}, {"current_file": "A.JPG", "new_name": "Two"}],
            [{"current_file": "../a.jpg", "new_name": "One"}],
        ):
            self.write_rows(rows)
            with self.assertRaises(RenameError):
                build_mapped_plan(self.folder, [self.a, self.b], self.manifest)

    def test_tied_dates_have_deterministic_natural_order(self):
        self.a.unlink()
        self.b.unlink()
        files = [self.folder / name for name in ("file10.jpg", "file2.jpg", "file1.jpg")]
        for path in files:
            path.write_bytes(b"file")
            os.utime(path, ns=(1_600_000_000_000000000, 1_600_000_000_000000000))
        expected = ["file1.jpg", "file2.jpg", "file10.jpg"]
        for order in ("Oldest first", "Newest first"):
            self.assertEqual([path.name for path in scan_files(self.folder, order=order)], expected)

    def test_created_and_modified_dates_are_distinct(self):
        os.utime(self.a, (10, 10))
        self.assertEqual(file_time_ns(self.a, "Modified"), self.a.stat().st_mtime_ns)
        if os.name == "nt" or hasattr(self.a.stat(), "st_birthtime_ns"):
            self.assertNotEqual(file_time_ns(self.a, "Created"), file_time_ns(self.a, "Modified"))
            plan = build_plan(self.folder, [self.a], ["New"], date_field="Created")
            self.assertEqual(plan.entries[0].timestamp_ns, file_time_ns(self.a, "Created"))
        else:
            with self.assertRaisesRegex(RenameError, "unavailable"):
                file_time_ns(self.a, "Created")


if __name__ == "__main__":
    unittest.main()
