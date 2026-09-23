"""Post-content editing for the dashboard: data/post.txt + data/car_photos.

This is the ONE sanctioned way to mutate posting material without leaving
the browser, so the path rules are strict by design:

* Everything lives under ``<project>/data/`` — resolved as the PARENT of the
  capture root (``repo/.local-capture`` -> ``repo/data``), the same tree both
  pipelines read (poster/main.py: cfg.post_text_file / cfg.photos_dir).
* Every write re-resolves its target and must land INSIDE that subtree:
  the dashboard has no auth (loopback pin is the only perimeter), so a
  ``../`` in a folder name must never reach ``open()``.
* The post file is written BYTE-EXACT modulo newline hygiene: CRLF is folded
  to LF (the poster splits on ``\\n``) and a trailing newline is ensured —
  the poster's 1:1 rule is about the bot not reformatting, not about the
  editor forbidding a final newline.
* Photo uploads: allow-listed image extensions + a size cap + basename-only
  file names. Thumbnails are served back from the same guard.
"""

from __future__ import annotations

import mimetypes
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# "01_car", "12_Tahoe LT" — leading number keeps folder order == car order
CAR_NAME_RE = re.compile(r"\d{1,3}_[A-Za-z0-9][A-Za-z0-9 _.-]{0,47}")
PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
MAX_PHOTO_BYTES = 25 * 1024 * 1024      # per file
MAX_POST_BYTES = 40 * 1024              # generous for a car-ad text, tiny for disk

_POST_NAME = "post.txt"
_PHOTOS_DIR_NAME = "car_photos"


def data_root(capture_root: Path) -> Path:
    return Path(capture_root).parent / "data"


def post_file(capture_root: Path) -> Path:
    return data_root(capture_root) / _POST_NAME


def photos_dir(capture_root: Path) -> Path:
    return data_root(capture_root) / _PHOTOS_DIR_NAME


class ContentError(ValueError):
    """Bad user input (name, size, type, path). Maps to HTTP 400."""


def _under(base: Path, candidate: Path) -> Path:
    """Resolve candidate and require it strictly INSIDE base."""
    root = Path(base).resolve()
    resolved = Path(candidate).expanduser().resolve()
    if root == resolved or root not in resolved.parents:
        raise ContentError(f"path escapes {root.name}/: {candidate.name!r}")
    return resolved


def _car_dir(capture_root: Path, car: str) -> Path:
    if not CAR_NAME_RE.fullmatch(car or ""):
        raise ContentError(
            f"bad folder name {car!r} — want NN_label, e.g. 04_tahoe")
    return _under(photos_dir(capture_root), photos_dir(capture_root) / car)


def _photo_path(capture_root: Path, car: str, name: str) -> Path:
    clean = Path(str(name or "")).name          # strips any directories
    if not clean or clean != name:
        raise ContentError(f"bad file name {name!r}")
    if Path(clean).suffix.lower() not in PHOTO_SUFFIXES:
        raise ContentError(
            f"{clean!r} is not an image (allowed: "
            + ", ".join(sorted(PHOTO_SUFFIXES)) + ")")
    target = _car_dir(capture_root, car) / clean
    return _under(photos_dir(capture_root), target)


def _mtime(path: Path) -> str | None:
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime,
                                  timezone.utc).isoformat(timespec="seconds")


# ---------- reads ----------

def read_content(capture_root: Path) -> dict[str, Any]:
    pf = post_file(capture_root)
    text: str | None = None
    if pf.is_file():
        text = pf.read_text(encoding="utf-8", errors="replace")
    cars: list[dict[str, Any]] = []
    pdir = photos_dir(capture_root)
    if pdir.is_dir():
        for d in sorted(pdir.iterdir()):
            if not d.is_dir():
                continue
            photos = [{"name": f.name, "bytes": f.stat().st_size}
                      for f in sorted(d.iterdir())
                      if f.is_file()
                      and f.suffix.lower() in PHOTO_SUFFIXES]
            cars.append({"name": d.name, "photos": photos})
    return {
        "post_text": text,
        "post_mtime": _mtime(pf),
        "post_exists": text is not None,
        "data_dir": str(data_root(capture_root)),
        "cars": cars,
    }


def resolve_image(capture_root: Path, car: str, name: str) -> Path:
    path = _photo_path(capture_root, car, name)
    if not path.is_file():
        raise ContentError(f"no such photo: {car}/{name}")
    return path


def image_content_type(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


# ---------- writes ----------

def save_post(capture_root: Path, text: str) -> dict[str, Any]:
    if not isinstance(text, str):
        raise ContentError("post text must be a string")
    body = text.replace("\r\n", "\n").replace("\r", "\n")
    if body and not body.endswith("\n"):
        body += "\n"
    raw = body.encode("utf-8")
    if len(raw) > MAX_POST_BYTES:
        raise ContentError(f"post text too large ({len(raw)} bytes, "
                           f"cap {MAX_POST_BYTES})")
    pf = post_file(capture_root)
    pf.parent.mkdir(parents=True, exist_ok=True)
    tmp = pf.with_suffix(".tmp")
    tmp.write_bytes(raw)
    tmp.replace(pf)
    return {"ok": True, "bytes": len(raw), "mtime": _mtime(pf)}


def add_car(capture_root: Path, car: str) -> dict[str, Any]:
    d = _car_dir(capture_root, car)
    if d.exists():
        raise ContentError(f"folder {car} already exists")
    d.parent.mkdir(parents=True, exist_ok=True)
    d.mkdir()
    return {"ok": True, "name": car}


def delete_car(capture_root: Path, car: str) -> dict[str, Any]:
    d = _car_dir(capture_root, car)
    if not d.is_dir():
        raise ContentError(f"no such folder: {car}")
    shutil.rmtree(d)
    return {"ok": True, "name": car}


def save_photo(capture_root: Path, car: str, name: str,
               blob: bytes) -> dict[str, Any]:
    if len(blob) > MAX_PHOTO_BYTES:
        raise ContentError(f"photo too large ({len(blob)} bytes, "
                           f"cap {MAX_PHOTO_BYTES})")
    if not blob:
        raise ContentError("empty upload")
    target = _photo_path(capture_root, car, name)
    if not target.parent.is_dir():
        raise ContentError(f"no such folder: {car} — create it first")
    target.write_bytes(blob)
    return {"ok": True, "car": car, "name": target.name,
            "bytes": len(blob)}


def delete_photo(capture_root: Path, car: str, name: str) -> dict[str, Any]:
    target = _photo_path(capture_root, car, name)
    if not target.is_file():
        raise ContentError(f"no such photo: {car}/{name}")
    target.unlink()
    return {"ok": True, "car": car, "name": target.name}
