"""Car photo collection: one sub-folder per car, deterministic order.

Order is a contract with the user (rule: photos must match post.txt order):
  cars  = immediate sub-folders of photos_dir, NAME-SORTED (use NN_ prefixes)
  photos within a car = filename-sorted, extensions filtered
Global cap AP_MAX_PHOTOS_PER_POST trims from the tail (never re-orders).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CarPhotos:
    """One car: its sub-folder name and its photos in upload order."""

    name: str
    dir: Path
    files: list[Path] = field(default_factory=list)


def collect_photos(
    photos_dir: Path, extensions: list[str] | tuple[str, ...], max_photos: int
) -> list[CarPhotos]:
    """Collect and order car photo folders; returns [] when dir absent/empty.

    Raises FileNotFoundError only if the dir exists as a FILE (real mistake).
    """
    exts = {e.lower() for e in extensions}
    if not photos_dir.exists():
        return []
    if not photos_dir.is_dir():
        raise FileNotFoundError(f"AP_PHOTOS_DIR is not a directory: {photos_dir}")

    cars: list[CarPhotos] = []
    for sub in sorted((p for p in photos_dir.iterdir() if p.is_dir()), key=lambda p: p.name):
        # skip hidden/metadata folders (.git, Obsidian junk etc.)
        if sub.name.startswith("."):
            continue
        files = sorted(
            (f for f in sub.iterdir() if f.is_file() and f.suffix.lower() in exts),
            key=lambda f: f.name,
        )
        # NOTE: iterdir (immediate files only), never rglob: if a car folder
        # contains nested sub-folders they are a config mistake and must not
        # silently merge into this car's photos.
        if files:
            cars.append(CarPhotos(name=sub.name, dir=sub, files=files))

    # apply the global cap without ever re-ordering
    budget = max(max_photos, 0)
    out: list[CarPhotos] = []
    for car in cars:
        if budget <= 0:
            break
        take = car.files[:budget]
        budget -= len(take)
        out.append(CarPhotos(name=car.name, dir=car.dir, files=take))
    return out
