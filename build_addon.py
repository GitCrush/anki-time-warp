#!/usr/bin/env python3
"""Build the distributable .ankiaddon files.

Produces two archives in ``dist/``:

``…-ankiweb.ankiaddon``   for the upload form on AnkiWeb, which assigns the
                          name and package itself and therefore wants no
                          manifest.json
``….ankiaddon``           for handing the file to someone directly or for
                          installing it locally; this one needs the manifest

The payload is checked before anything is written: every file present, no
bytecode, the version taken from the manifest.

Run with:  python3 build_addon.py
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
MANIFEST = "manifest.json"
NAME = "anki-time-warp"

# Everything that belongs in the add-on, in the order it is written.
PAYLOAD = [
    "__init__.py",
    "core.py",
    "ui.py",
    "tag_input_widget.py",
    "chart.min.js",
    "logo.png",
]

_failed = False


def fail(message: str) -> None:
    global _failed
    _failed = True
    print(f"  ✗ {message}")


def check() -> str:
    print("checking…")
    missing = [name for name in PAYLOAD + [MANIFEST] if not (ROOT / name).is_file()]
    if missing:
        fail(f"missing from the working tree: {', '.join(missing)}")
    manifest = json.loads((ROOT / MANIFEST).read_text(encoding="utf-8"))
    for key in ("name", "version"):
        if key not in manifest:
            fail(f"{MANIFEST} has no {key!r}")
    if any(part == "__pycache__" for name in PAYLOAD for part in Path(name).parts):
        fail("payload contains bytecode")
    if _failed:
        raise SystemExit(1)
    version = str(manifest["version"])
    print(f"  ✓ {len(PAYLOAD)} files, version {version}")
    return version


def build(version: str) -> None:
    DIST.mkdir(exist_ok=True)
    print("building…")
    for suffix, with_manifest in (("-ankiweb", False), ("", True)):
        target = DIST / f"{NAME}-{version}{suffix}.ankiaddon"
        entries = PAYLOAD + ([MANIFEST] if with_manifest else [])
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in entries:
                archive.write(ROOT / name, name)
        size = target.stat().st_size
        print(f"  ✓ {target.relative_to(ROOT)}  ({size / 1024:.0f} KB, {len(entries)} files)"
              f" — {'direct install' if with_manifest else 'AnkiWeb upload'}")
    print("\ndone. Upload the -ankiweb file at https://ankiweb.net/shared/mine")


if __name__ == "__main__":
    build(check())
