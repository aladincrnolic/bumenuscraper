
"""Synthetic-only tests for cleanup_archives.py. Never touch real archives."""

import contextlib
import io
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from cleanup_archives import cleanup_archives, find_expired_archives, main


class CleanupArchivesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        self.root = Path(self.temp.name)
        self.archive = self.root / "data" / "archive"
        self.archive.mkdir(parents=True)

        self.current = self.root / "data" / "current" / "menus.json"
        self.current.parent.mkdir()
        self.current.write_text("original menu", encoding="utf-8")

        self.staging = self.root / "data" / "staging" / "working.json"
        self.staging.parent.mkdir()
        self.staging.write_text("in progress", encoding="utf-8")

        self.now = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)

    def create_file(self, name, age_days, payload=b"12345"):
        path = self.archive / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

        modified = (self.now - timedelta(days=age_days)).timestamp()
        os.utime(path, (modified, modified))
        return path

    def scan(self, **overrides):
        settings = dict(archive_dir=self.archive, now=self.now)
        settings.update(overrides)
        return cleanup_archives(**settings)

    def test_newer_than_30_days_preserved(self):
        file = self.create_file("recent.json", 29)
        report = self.scan(dry_run=False)
        self.assertTrue(file.exists())
        self.assertEqual(report["expired_files"], 0)

    def test_exactly_30_days_preserved(self):
        file = self.create_file("exact.json", 30)
        self.assertEqual(self.scan(dry_run=False)["deleted_files"], 0)
        self.assertTrue(file.exists())

    def test_older_than_30_days_deleted(self):
        file = self.create_file("old.json", 31)
        report = self.scan(dry_run=False)
        self.assertFalse(file.exists())
        self.assertEqual(report["deleted_files"], 1)

    def test_mixed_ages(self):
        files = [
            self.create_file(f"{age}.json", age)
            for age in (3, 30, 31, 60)
        ]
        report = self.scan(dry_run=False)
        self.assertEqual(report["deleted_files"], 2)
        self.assertEqual(
            [p.exists() for p in files],
            [True, True, False, False]
        )

    def test_default_is_dry_run(self):
        file = self.create_file("old.json", 50)
        report = self.scan()
        self.assertTrue(report["dry_run"])
        self.assertTrue(file.exists())
        self.assertEqual(report["deleted_files"], 0)
        self.assertEqual(report["expired_files"], 1)

    def test_cli_delete_removes_only_expired(self):
        old = self.create_file("old.json", 40)
        new = self.create_file("new.json", 1)

        # CLI uses the actual clock; set timestamps accordingly.
        os.utime(old, (0, 0))
        recent = datetime.now(timezone.utc).timestamp() - 60
        os.utime(new, (recent, recent))

        with contextlib.redirect_stdout(io.StringIO()):
            code = main([
                "--archive-dir", str(self.archive), "--delete"
            ])

        self.assertEqual(code, 0)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())

    def test_empty_archive(self):
        report = self.scan(dry_run=False)
        self.assertEqual(report["files_scanned"], 0)
        self.assertEqual(report["errors"], [])

    def test_missing_archive(self):
        self.archive.rmdir()
        report = self.scan(dry_run=False)
        self.assertEqual(report["files_scanned"], 0)
        self.assertTrue(report["warnings"])
        self.assertEqual(report["errors"], [])

    def test_custom_retention(self):
        file = self.create_file("older.json", 40)
        self.assertEqual(
            self.scan(retention_days=45)["expired_files"], 0
        )
        self.assertEqual(
            self.scan(retention_days=35)["expired_files"], 1
        )
        self.assertTrue(file.exists())

    def test_bad_retention(self):
        for bad in (0, -1, "abc", 1.3, True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.scan(retention_days=bad)

        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--retention-days", "0"]), 2)

    def test_nested_folders(self):
        old = self.create_file("legacy/2026/old.json", 35)
        self.assertEqual(
            self.scan(dry_run=False)["deleted_files"], 1
        )
        self.assertFalse(old.exists())
        self.assertTrue(
            (self.archive / "legacy" / "2026").exists()
        )

    def test_html_json_and_htm(self):
        for name in ("west.html", "warren.htm", "menus.json"):
            self.create_file(name, 35)
        self.assertEqual(
            self.scan(dry_run=False)["deleted_files"], 3
        )

    def test_unknown_extension_preserved(self):
        other = self.create_file("notes.txt", 50)
        report = self.scan(dry_run=False)
        self.assertTrue(other.exists())
        self.assertEqual(report["skipped_files"], 1)

    def test_external_symlink_file_ignored(self):
        outside = self.root / "outside.json"
        outside.write_text("safe", encoding="utf-8")
        link = self.archive / "outside.json"

        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("Cannot create symlinks on this platform")

        self.scan(dry_run=False)
        self.assertTrue(outside.exists())
        self.assertTrue(link.is_symlink())

    def test_external_symlink_directory_ignored(self):
        other = self.root / "external"
        other.mkdir()

        victim = other / "old.json"
        victim.write_text("outside")
        link = self.archive / "badlink"

        try:
            link.symlink_to(other, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Cannot create directory symlinks on this platform")

        report = self.scan(dry_run=False)
        self.assertTrue(victim.exists())
        self.assertTrue(report["file_details"])

    def test_target_current_rejected(self):
        with self.assertRaises(ValueError):
            cleanup_archives(
                archive_dir=self.current.parent,
                now=self.now,
                dry_run=False
            )

    def test_project_root_and_data_rejected(self):
        for unsafe in (
            self.root,
            self.root / "data",
            self.root / "data" / "staging"
        ):
            with self.subTest(unsafe=unsafe), self.assertRaises(ValueError):
                cleanup_archives(
                    archive_dir=unsafe,
                    now=self.now,
                    dry_run=False
                )

    def test_invalid_timestamp_skipped(self):
        file = self.create_file("old.json", 50)
        original_lstat = Path.lstat

        def fail_for_target(path):
            if path == file:
                raise OSError("simulated failure")
            return original_lstat(path)

        with mock.patch.object(
            Path,
            "lstat",
            autospec=True,
            side_effect=fail_for_target
        ):
            report = self.scan(dry_run=False)

        self.assertTrue(file.exists())
        self.assertTrue(report["errors"])
        self.assertEqual(report["deleted_files"], 0)

    def test_future_timestamp_skipped(self):
        file = self.create_file("future.json", -1)
        self.assertEqual(
            self.scan(dry_run=False)["deleted_files"], 0
        )
        self.assertTrue(file.exists())

    def test_deletion_failure_reported(self):
        file = self.create_file("old.json", 40)

        with mock.patch.object(
            Path,
            "unlink",
            autospec=True,
            side_effect=PermissionError("access denied")
        ):
            report = self.scan(dry_run=False)

        self.assertEqual(report["deleted_files"], 0)
        self.assertEqual(report["failed_files"], 1)
        self.assertTrue(report["errors"])
        self.assertTrue(file.exists())

    def test_month_and_year_boundary(self):
        self.now = datetime(2027, 1, 1, tzinfo=timezone.utc)
        too_old = self.create_file("november.json", 32)
        exact = self.create_file("december.json", 30)

        report = self.scan(dry_run=False)
        self.assertEqual(report["deleted_files"], 1)
        self.assertFalse(too_old.exists())
        self.assertTrue(exact.exists())

    def test_accurate_byte_reporting(self):
        self.create_file("a.html", 40, b"123")
        self.create_file("b.json", 40, b"1234567")
        self.create_file("new.json", 2, b"xx")

        preview = self.scan()
        self.assertEqual(preview["files_scanned"], 3)
        self.assertEqual(preview["expired_files"], 2)
        self.assertEqual(preview["eligible_bytes"], 10)
        self.assertEqual(preview["deleted_bytes"], 0)

        real = self.scan(dry_run=False)
        self.assertEqual(real["deleted_bytes"], 10)
        self.assertEqual(real["skipped_files"], 1)

    def test_current_unchanged(self):
        self.create_file("old.json", 50)
        self.scan(dry_run=False)

        self.assertEqual(
            self.current.read_text(encoding="utf-8"),
            "original menu"
        )
        self.assertEqual(
            self.staging.read_text(encoding="utf-8"),
            "in progress"
        )

    def test_outside_file_untouched(self):
        outside = self.root / "important.json"
        outside.write_text("do not delete")
        os.utime(outside, (0, 0))

        self.create_file("inside.json", 50)
        self.scan(dry_run=False)
        self.assertTrue(outside.exists())

    def test_known_run_directory(self):
        run = "2026-09-01_120000000000_deadbeef"
        old = self.create_file(f"{run}/west.html", 40)
        other = self.create_file(f"{run}/menus.json", 40)

        report = self.scan(dry_run=False)
        self.assertEqual(report["deleted_files"], 2)
        self.assertFalse(old.exists())
        self.assertFalse(other.exists())
        self.assertTrue((self.archive / run).exists())

    def test_failed_run_directory(self):
        run = "failed_2026-09-01_120000000000_deadbeef"
        path = self.create_file(f"{run}/validation.json", 40)

        self.assertEqual(
            self.scan(dry_run=False)["deleted_files"], 1
        )
        self.assertFalse(path.exists())

    def test_recent_run_id_blocks_old_mtime(self):
        run = "2026-11-06_120000000000_deadbeef"
        path = self.create_file(f"{run}/west.html", 80)

        report = self.scan(dry_run=False)
        self.assertEqual(report["deleted_files"], 0)
        self.assertTrue(path.exists())

    def test_unknown_entry_blocks_entire_run(self):
        run = "2026-09-01_120000000000_deadbeef"
        old = self.create_file(f"{run}/west.html", 50)
        note = self.create_file(f"{run}/README.txt", 50)

        report = self.scan(dry_run=False)
        self.assertEqual(report["deleted_files"], 0)
        self.assertTrue(old.exists())
        self.assertTrue(note.exists())

    def test_new_file_blocks_entire_run(self):
        run = "2026-09-01_120000000000_deadbeef"
        old = self.create_file(f"{run}/west.html", 50)
        recent = self.create_file(f"{run}/menus.json", 1)

        self.assertEqual(
            self.scan(dry_run=False)["deleted_files"], 0
        )
        self.assertTrue(old.exists())
        self.assertTrue(recent.exists())

    def test_malformed_run_id_skipped(self):
        invalid = self.create_file(
            "failed_2026-99-01_120000000000_deadbeef/west.html",
            50
        )
        self.assertEqual(
            self.scan(dry_run=False)["deleted_files"], 0
        )
        self.assertTrue(invalid.exists())

    def test_find_is_read_only(self):
        old = self.create_file("old.json", 45)
        found = find_expired_archives(
            self.archive, now=self.now
        )

        self.assertEqual(len(found["expired"]), 1)
        self.assertEqual(found["expired"][0]["size_bytes"], 5)
        self.assertTrue(old.exists())

    def test_naive_now_rejected(self):
        with self.assertRaises(ValueError):
            self.scan(now=datetime(2026, 11, 7))

    def test_file_changed_during_delete_is_skipped(self):
        file = self.create_file("old.json", 40)
        real_lstat = Path.lstat
        count = {str(file): 0}

        def swapped_lstat(path):
            if str(path) == str(file):
                count[str(file)] += 1
                if count[str(file)] == 2:
                    path.write_bytes(b"new content")
            return real_lstat(path)

        with mock.patch.object(
            Path,
            "lstat",
            autospec=True,
            side_effect=swapped_lstat
        ):
            report = self.scan(dry_run=False)

        self.assertEqual(report["deleted_files"], 0)
        self.assertTrue(file.exists())


if __name__ == "__main__":
    unittest.main()
