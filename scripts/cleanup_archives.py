
"""Safely retain only recent BU Dining archive files. No external packages."""

import argparse
import os
import re
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ARCHIVE_DIR = PROJECT_ROOT / "data" / "archive"
APPROVED_EXTENSIONS = {".html", ".htm", ".json"}
RUN_ID_PATTERN = re.compile(
    r"(?:failed_)?(?P<timestamp>\d{4}-\d{2}-\d{2}_\d{12})_[0-9a-fA-F]{8}"
)


def _settings(archive_dir, retention_days, now):
    """Validate inputs and reject paths that could touch operational data."""
    if type(retention_days) is not int or retention_days <= 0:
        raise ValueError("retention_days must be a positive whole number")
    if now is None:
        now = datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    now = now.astimezone(timezone.utc)

    path = Path(archive_dir) if archive_dir is not None else DEFAULT_ARCHIVE_DIR
    path = path.expanduser().absolute()

    # Reject symlinks in the archive path itself (including parent directories).
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("archive directory or one of its parents is a symlink")

    path = path.resolve(strict=False)
    if path.name.lower() not in {"archive", "archives"}:
        raise ValueError("archive directory must end in 'archive' or 'archives'")
    if any(part.lower() in {"current", "staging", "scripts"} for part in path.parts):
        raise ValueError("archive directory is inside a protected location")
    if path.is_relative_to(PROJECT_ROOT) and not path.is_relative_to(DEFAULT_ARCHIVE_DIR):
        raise ValueError("inside this project, only data/archive/ is permitted")

    return path, retention_days, now


def _run_group(archive_root, file_path):
    """Return (group path, UTC run timestamp, error) for run-ID directories."""
    relative = file_path.relative_to(archive_root)
    if len(relative.parts) < 2:
        return None, None, None

    name = relative.parts[0]
    match = RUN_ID_PATTERN.fullmatch(name)

    if match:
        try:
            stamped = datetime.strptime(
                match.group("timestamp"), "%Y-%m-%d_%H%M%S%f"
            ).replace(tzinfo=timezone.utc)
            return archive_root / name, stamped, None
        except ValueError:
            return archive_root / name, None, "invalid run-ID timestamp"

    if name.startswith("failed_") or re.match(r"\d{4}-\d{2}-\d{2}_", name):
        return archive_root / name, None, "unrecognized run-ID directory"

    return None, None, None


def _symlink_in_relative_path(root, path):
    """Check for symlinks again immediately before each deletion."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True

    current = root
    if any(part.is_symlink() for part in (root, *root.parents)):
        return True

    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            return True

    return False


def find_expired_archives(archive_dir=None, retention_days=30, now=None):
    """Scan without changing files; return expired candidates and audit details."""
    root, retention_days, now = _settings(archive_dir, retention_days, now)
    cutoff = now - timedelta(days=retention_days)

    result = {
        "archive_dir": str(root),
        "files_scanned": 0,
        "expired": [],
        "file_details": [],
        "errors": [],
        "warnings": [],
    }

    if not root.exists():
        result["warnings"].append("Archive directory does not exist; nothing to do")
        return result

    if not root.is_dir():
        result["errors"].append(f"Not a directory: {root}")
        return result

    blocked_groups = set()
    fingerprints = {}  # Used only to detect changes before deletion.
    entries = result["file_details"]

    def walk_error(error):
        result["errors"].append(f"Could not scan {error.filename}: {error}")
        try:
            group, _, _ = _run_group(root, Path(error.filename) / "placeholder")
            if group is not None:
                blocked_groups.add(group)
        except (ValueError, TypeError):
            pass

    for folder, directories, filenames in os.walk(root, topdown=True,
                                                    followlinks=False,
                                                    onerror=walk_error):
        folder = Path(folder)

        for name in sorted(directories):
            child = folder / name
            if child.is_symlink():
                group, _, _ = _run_group(root, child / "placeholder")
                if group is not None:
                    blocked_groups.add(group)

                entries.append({
                    "path": str(child),
                    "kind": "directory",
                    "status": "skipped",
                    "reason": "symbolic-link directory"
                })
                directories.remove(name)

        directories.sort()

        for name in sorted(filenames):
            file_path = folder / name
            result["files_scanned"] += 1

            detail = {
                "path": str(file_path),
                "kind": "file",
                "status": "skipped",
                "reason": ""
            }
            entries.append(detail)

            group, run_time, group_error = _run_group(root, file_path)

            if group_error:
                blocked_groups.add(group)
                detail["reason"] = group_error
                continue

            try:
                info = file_path.lstat()  # Never follow a symlink.

                if not stat.S_ISREG(info.st_mode):
                    detail["reason"] = "not a regular file (or is a symlink)"

                elif file_path.suffix.lower() not in APPROVED_EXTENSIONS:
                    detail["reason"] = "unapproved file extension"

                else:
                    modified = datetime.fromtimestamp(
                        info.st_mtime, tz=timezone.utc
                    )

                    detail["modified_at_utc"] = modified.isoformat()
                    detail["size_bytes"] = info.st_size
                    detail["age_days"] = round(
                        (now - modified).total_seconds() / 86400, 6
                    )

                    if modified > now:
                        detail["reason"] = "future modification timestamp"

                    elif modified >= cutoff:
                        detail["reason"] = "within retention period"

                    elif group is not None and (
                        run_time is None or run_time >= cutoff
                    ):
                        detail["reason"] = "run timestamp is within retention period"

                    else:
                        detail["status"] = "candidate"
                        fingerprints[str(file_path)] = (
                            info.st_dev,
                            info.st_ino,
                            info.st_size,
                            info.st_mtime_ns
                        )

            except (OSError, OverflowError, ValueError) as exc:
                detail["status"] = "failed"
                detail["reason"] = f"could not examine file: {exc}"
                result["errors"].append(f"{file_path}: {exc}")

            if group is not None and detail["status"] != "candidate":
                blocked_groups.add(group)

    for detail in entries:
        if detail["status"] != "candidate":
            continue

        group, _, _ = _run_group(root, Path(detail["path"]))

        if group in blocked_groups:
            detail["status"] = "skipped"
            detail["reason"] = "snapshot contains a recent, unknown, or unsafe entry"

        else:
            detail["status"] = "eligible"
            detail["reason"] = "older than retention period"
            result["expired"].append({
                key: detail[key]
                for key in ("path", "size_bytes", "age_days", "modified_at_utc")
            })

    result["_fingerprints"] = fingerprints  # Private implementation detail.
    return result


def cleanup_archives(archive_dir=None, retention_days=30, dry_run=True, now=None):
    """Find or safely delete eligible files; dry_run is True by default."""
    scan = find_expired_archives(archive_dir, retention_days, now)

    root = Path(scan["archive_dir"])
    details = scan["file_details"]

    result = {
        "dry_run": dry_run,
        "retention_days": retention_days,
        "files_scanned": scan["files_scanned"],
        "expired_files": len(scan["expired"]),
        "deleted_files": 0,
        "skipped_files": 0,
        "failed_files": 0,
        "eligible_bytes": sum(item["size_bytes"] for item in scan["expired"]),
        "deleted_bytes": 0,
        "errors": scan["errors"],
        "warnings": scan["warnings"],
        "file_details": details,
    }

    if not dry_run:
        for detail in details:
            if detail["status"] != "eligible":
                continue

            path = Path(detail["path"])

            try:
                if _symlink_in_relative_path(root, path):
                    detail["status"] = "skipped"
                    detail["reason"] = "path became a symlink or escaped archive"
                    continue

                info = path.lstat()
                actual = (
                    info.st_dev,
                    info.st_ino,
                    info.st_size,
                    info.st_mtime_ns
                )

                if (
                    not stat.S_ISREG(info.st_mode)
                    or actual != scan["_fingerprints"][str(path)]
                ):
                    detail["status"] = "skipped"
                    detail["reason"] = "file changed since scan"
                    continue

                path.unlink()
                detail["status"] = "deleted"
                detail["reason"] = "deleted successfully"

                result["deleted_files"] += 1
                result["deleted_bytes"] += detail["size_bytes"]

            except OSError as exc:
                detail["status"] = "failed"
                detail["reason"] = f"deletion failed: {exc}"
                result["errors"].append(f"Could not delete {path}: {exc}")

    result["skipped_files"] = sum(
        d["kind"] == "file" and d["status"] == "skipped"
        for d in details
    )
    result["failed_files"] = sum(
        d["kind"] == "file" and d["status"] == "failed"
        for d in details
    )

    return result


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Remove old BU menu archives safely"
    )
    parser.add_argument("--archive-dir", type=Path, default=None)
    parser.add_argument("--retention-days", type=int, default=30)

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--delete", action="store_true", help="actually delete files"
    )
    mode.add_argument(
        "--dry-run", action="store_true", help="preview only (default)"
    )

    args = parser.parse_args(argv)

    try:
        report = cleanup_archives(
            args.archive_dir,
            args.retention_days,
            dry_run=not args.delete
        )
    except (ValueError, OSError, OverflowError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(f"Mode: {'DRY RUN' if report['dry_run'] else 'DELETE'}")

    for key in (
        "files_scanned", "expired_files", "deleted_files",
        "skipped_files", "failed_files", "eligible_bytes",
        "deleted_bytes"
    ):
        print(f"{key}: {report[key]}")

    for detail in report["file_details"]:
        print(
            f"  [{detail['status']}] {detail['path']}: {detail['reason']}"
        )

    for warning in report["warnings"]:
        print(f"WARNING: {warning}")

    for error in report["errors"]:
        print(f"ERROR: {error}", file=sys.stderr)

    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
