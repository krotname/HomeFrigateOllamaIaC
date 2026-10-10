"""Apply the bounded, resumable frame fix to an existing Windows phone OCR worker."""

import argparse
import ast
import os
from pathlib import Path


OLD_VERSION = "2026-10-01-text-json-v5"
NEW_VERSION = "2026-10-08-frame-resume-v6"
REPLACEMENTS = (
    (f'WORKER_VERSION = "{OLD_VERSION}"', f'WORKER_VERSION = "{NEW_VERSION}"'),
    (
        'if frames > 200 or len(data) > MAX_BYTES:',
        'if frames > 1 or len(data) > MAX_BYTES:',
    ),
    (
        'if depth > 0 and height <= 1024 else None',
        'if height <= 1024 else None',
    ),
    (
        'order = "priority,attempts,rowid" if only_failed else "priority,rowid"\n',
        'order = "priority,attempts,rowid"\n',
    ),
)


def repaired_source(source, *, rollback=False):
    """Reject unknown or partially changed workers before touching the live file."""
    replacements = tuple((new, old) for old, new in REPLACEMENTS) if rollback else REPLACEMENTS
    if all(old not in source and source.count(new) == 1 for old, new in replacements):
        ast.parse(source)
        return source
    if not all(source.count(old) == 1 and new not in source for old, new in replacements):
        raise ValueError("Unsupported worker revision; no changes applied")
    for old, new in replacements:
        source = source.replace(old, new, 1)
    ast.parse(source)
    return source


def repair(path, *, rollback=False):
    path = Path(path)
    source = path.read_text(encoding="utf-8")
    updated = repaired_source(source, rollback=rollback)
    if updated == source:
        return False
    temporary = path.with_name(path.name + ".frame-repair.tmp")
    try:
        temporary.write_text(updated, encoding="utf-8", newline="")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("worker", type=Path)
    parser.add_argument("--rollback", action="store_true")
    args = parser.parse_args()
    changed = repair(args.worker, rollback=args.rollback)
    print("updated" if changed else "already updated")


if __name__ == "__main__":
    main()
