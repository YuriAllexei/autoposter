"""Per-listing group selection (the dashboard's "Groups matrix").

WHAT IT IS: for one marketplace listing, the set of joined groups the operator
TURNED OFF. `poster.share` filters its per-listing plan with it; the dashboard
renders it as checkboxes.

FAIL-SAFE DIRECTION (deliberate): the file stores what is OFF, never what is ON.
A group joined later, a listing with no entry, a missing or corrupt file — all of
them mean "everything enabled". A mistake here can only cost a missed share,
never post a listing into a group the operator did not allow.

LAYOUT: <capture_root>/config/group_matrix.json
{ "version": 1, "updated_at": "...", "listings": {"1178670714486639": {"disabled": ["venta de carros chihuahua"]}} }
KEYS ARE FOLDED (fold_name) so case/accent/whitespace can never make a checkbox
miss the group the pipeline is about to address.
"""
from __future__ import annotations

import json
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

#: schema version of the on-disk file. Bump only with a migration path.
VERSION = 1

#: subdirectory (under capture_root) that holds the matrix file
_CONFIG_DIRNAME = "config"
_MATRIX_FILENAME = "group_matrix.json"


class MatrixError(ValueError):
    """A caller handed an unusable listing id or a non-list `disabled`."""


def fold_name(name: object) -> str:
    """Fold a group name for comparison: NFC + collapse whitespace + casefold.

    Byte-identical to `poster.flows._norm` (the pipeline's own name matcher) —
    a test pins the parity. The fold direction is deliberate: the operator's
    checkbox and the row the pipeline is about to address must collapse to the
    same key no matter the case, accents (NFC/NFD) or line breaks in either.
    """
    return " ".join(unicodedata.normalize("NFC", str(name)).split()).casefold()


def matrix_path(capture_root) -> Path:
    """<capture_root>/config/group_matrix.json (capture_root is caller-owned:
    production = repo/.local-capture, tests = tmp_path)."""
    return Path(capture_root) / _CONFIG_DIRNAME / _MATRIX_FILENAME


def read_matrix(capture_root) -> dict:
    """Read the whole matrix. NEVER raises: a missing, unreadable, corrupt or
    non-object file collapses to an EMPTY, everything-enabled matrix (plus an
    'error' string explaining why) so a broken file can only cost missed
    shares, never an unintended post."""
    path = matrix_path(capture_root)
    result: dict = {
        "version": VERSION,
        "updated_at": "",
        "listings": {},
        "path": str(path),
    }
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return result
    except OSError as e:
        result["error"] = f"{type(e).__name__}: {e}"
        return result

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as e:
        result["error"] = f"corrupt group matrix: {e}"
        return result
    if not isinstance(data, dict):
        result["error"] = "corrupt group matrix: top level is not a JSON object"
        return result

    listings: dict[str, dict] = {}
    raw_listings = data.get("listings")
    if isinstance(raw_listings, dict):
        for lid, entry in raw_listings.items():
            if not isinstance(entry, dict):
                continue
            disabled = entry.get("disabled")
            if isinstance(disabled, list):
                listings[str(lid)] = {"disabled": [str(x) for x in disabled]}

    result["version"] = data.get("version", VERSION)
    result["updated_at"] = data.get("updated_at", "")
    result["listings"] = listings
    return result


def disabled_for(capture_root, listing_id: object) -> set[str]:
    """Folded names switched OFF for one listing. Missing file, corrupt file,
    unknown listing id and a group joined later all yield the empty set =>
    everything enabled. Never raises."""
    lid = str(listing_id)
    entry = read_matrix(capture_root)["listings"].get(lid)
    if not isinstance(entry, dict):
        return set()
    disabled = entry.get("disabled")
    if not isinstance(disabled, list):
        return set()
    # fold on the way out too: a hand-edited file may carry raw names and the
    # planner folds plan rows before comparing (both sides must be folded).
    return {fold_name(x) for x in disabled}


def filter_plan(plan: list, disabled: set[str]) -> tuple[list, list]:
    """Split a per-listing plan into (kept, dropped) by folded group name.

    `disabled` is a set of ALREADY-FOLDED names (see disabled_for). Rows whose
    folded name is disabled are dropped; every other row — including names the
    matrix has never heard of — is KEPT (fail-safe direction). With nothing
    disabled this is a true no-op: the SAME list comes back, nothing copied."""
    if not disabled:
        return plan, []
    kept: list = []
    dropped: list = []
    for row in plan:
        name = row.get("name") if isinstance(row, dict) else None
        (dropped if fold_name(name) in disabled else kept).append(row)
    return kept, dropped


def _validate_listing_id(listing_id: object) -> str:
    """A usable listing id is a non-empty, all-digit string (the recordings'
    ids like '1178670714486639'). Everything else is a caller bug."""
    lid = str(listing_id)
    if not lid.isdigit():
        raise MatrixError(
            f"invalid listing_id {listing_id!r}: expected a non-empty "
            "all-digit string")
    return lid


def save_matrix(capture_root, listing_id: object, disabled: object) -> dict:
    """Replace ONE listing's OFF set and return a fresh read_matrix().

    `listing_id` must be a non-empty all-digit string and `disabled` must be a
    list; anything else raises MatrixError. Names are folded + deduped before
    writing, other listings are preserved, and the file is written atomically
    (tmp + Path.replace) so a crash can never leave a half-written matrix."""
    lid = _validate_listing_id(listing_id)
    if not isinstance(disabled, list):
        raise MatrixError(
            f"invalid disabled {disabled!r}: expected a list of group names")

    names = sorted({fold_name(x) for x in disabled})

    current = read_matrix(capture_root)
    listings = dict(current["listings"])
    listings[lid] = {"disabled": names}

    payload = {
        "version": VERSION,
        "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "listings": listings,
    }
    path = matrix_path(capture_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)
    return read_matrix(capture_root)
