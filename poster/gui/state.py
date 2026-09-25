"""Read-only views over the autoposter's on-disk artifacts.

WHY parse files instead of importing the browser code: the bot owns ONE
persistent Firefox profile (`.local-capture/profiles/facebook`) and a second
process touching that directory corrupts the cookie DB and fights over the
profile lock. The dashboard therefore never launches a browser; it only reads
what finished runs left behind, and live actions are handed to the CLI as
subprocesses.

Artifacts (all relative to the capture root = `Config.screenshot_dir.parent`):

  groups/joined_<UTCstamp>.json   newest = joined-groups snapshot, written by
                                  `python -m poster.main --list-groups`
  cache/listings.json             active marketplace listings snapshot
                                  (`poster.listings.save_listings_snapshot`)
  results/ledger.jsonl            one attempt per line, appended by
                                  `poster.results.RunRecorder`
  logs/run_<UTCstamp>.log         stdout of each CLI run

The groups snapshot is discovered as "the most recently MODIFIED of
`cache/groups.json` (if that path ever lands) and `groups/joined_*.json`" —
the GUI must show the freshest truth on disk regardless of which writer
produced it. Every artifact is optional: a missing file is reported as
`available: false` ("not fetched yet"), never as an error and never as "0
groups" (a failed joins fetch must not read as "the account joined nothing").

The ledger is parsed TOLERANTLY because two record schemas share one file:
a line with `group_id` is a group posting attempt, a line with `listing_id`
is a crosspost batch. Anything else — blank lines, truncated writes, JSON
that is not an object — is skipped without failing the view.
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..results import (
    STATUS_FAILED,
    STATUS_SKIPPED,
    last_attempt_ts,
    record_kind,
    select_targets,
)

# Ledger record kinds. `unknown` keeps forward compatibility: a future schema
# shows up in counts without crashing the dashboard. The kind is read from the
# row's explicit `kind` field when present (poster.results writes one on every
# row); rows written before that field existed are inferred from their ids.
KIND_GROUP = "group"
KIND_CROSSPOST = "crosspost"
KIND_SHARE = "share"
KIND_UNKNOWN = "unknown"

RECENT_LEDGER_LIMIT = 50
RECENT_RUN_LIMIT = 15
RECENT_LOG_LIMIT = 15


@dataclass(frozen=True)
class GuiPaths:
    """Every file/dir the dashboard reads, resolved once at startup.

    `from_root` exists so tests can point the whole view at a tmp tree
    without building a Config (which reads .env).
    """

    capture_root: Path

    @property
    def ledger(self) -> Path:
        return self.capture_root / "results" / "ledger.jsonl"

    @property
    def listings(self) -> Path:
        return self.capture_root / "cache" / "listings.json"

    @property
    def groups_dir(self) -> Path:
        return self.capture_root / "groups"

    @property
    def fixed_groups(self) -> Path:
        return self.capture_root / "cache" / "groups.json"

    @property
    def logs_dir(self) -> Path:
        return self.capture_root / "logs"

    @classmethod
    def from_root(cls, root: Path) -> GuiPaths:
        return cls(capture_root=Path(root))

    @classmethod
    def from_config(cls, cfg: Any) -> GuiPaths:
        """Capture root is the parent of the screenshot dir — the same anchor
        `poster.main --list-groups` and `poster.listings` write under."""
        return cls(capture_root=Path(cfg.screenshot_dir).parent)


def identity_view(cfg: Any) -> dict[str, Any]:
    """The header facts about the posting identity + run caps (no secrets)."""
    return {
        "label": cfg.identity_label,
        "id": cfg.identity_id,
        "post_as": cfg.post_as,
        "env_dry_run": bool(cfg.dry_run),
        "max_posts_per_run": int(cfg.max_posts_per_run),
    }


# --------------------------------------------------------------------------
# tolerance helpers
# --------------------------------------------------------------------------

def _as_list(value: Any) -> list[str]:
    """Crosspost fields arrive as `[]`, a bare string, or absent."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, Iterable):
        return [str(v) for v in value]
    return [str(value)]


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_str(value: Any) -> str:
    return "" if value is None else str(value)


@dataclass(frozen=True)
class LedgerLine:
    """One parsed ledger record, normalised across both schemas."""

    kind: str
    run_id: str
    ts: str
    status: str
    error: str | None
    dry_run: bool | None
    # group-posting fields
    group_id: str
    name: str
    # crosspost fields
    listing_id: str
    listing_title: str
    batch: int | None
    group_ids: list[str]
    group_names: list[str]
    count: int | None
    # delivery verdict on real publishes: "live" | "pending" | "unknown"
    # (None on every other row) — the dashboard's "live?" column
    delivered: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "run_id": self.run_id, "ts": self.ts,
            "status": self.status, "error": self.error, "dry_run": self.dry_run,
            "group_id": self.group_id, "name": self.name,
            "listing_id": self.listing_id, "listing_title": self.listing_title,
            "batch": self.batch, "group_ids": self.group_ids,
            "group_names": self.group_names, "count": self.count,
            "delivered": self.delivered,
        }


def parse_ledger_line(raw: str) -> LedgerLine | None:
    """Tolerant single-line parse; None for blank/corrupt/non-object lines.

    The row's OWN `kind` field is authoritative (poster.results stamps one on
    every line). Only when it is absent — rows written before the field
    existed — is the kind inferred from ids: `group_id` (checked FIRST, the
    ordering the original contract states) means a group-posting attempt,
    otherwise `listing_id` means a crosspost; neither means `unknown`, which
    is counted but never interpreted.

    This matters because a SHARE row carries BOTH `group_id` and `listing_id`;
    inferring from ids would file it as a text-group attempt and let it push
    the Joined-groups rotation clock (the C0 bug).
    """
    if not raw or not raw.strip():
        return None
    try:
        rec = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(rec, dict):
        return None
    kind = record_kind(rec)
    group_id = _as_str(rec.get("group_id")).strip()
    listing_id = _as_str(rec.get("listing_id")).strip()
    dry = rec.get("dry_run")
    return LedgerLine(
        kind=kind,
        run_id=_as_str(rec.get("run_id")),
        ts=_as_str(rec.get("ts")),
        status=_as_str(rec.get("status")),
        error=(None if rec.get("error") in (None, "") else _as_str(rec["error"])),
        dry_run=(bool(dry) if isinstance(dry, bool) else None),
        group_id=group_id,
        name=_as_str(rec.get("name")),
        listing_id=listing_id,
        listing_title=_as_str(rec.get("listing_title")),
        batch=_as_int(rec.get("batch")),
        group_ids=_as_list(rec.get("group_ids")),
        group_names=_as_list(rec.get("group_names")),
        count=_as_int(rec.get("count")),
        delivered=(None if rec.get("delivered") in (None, "")
                   else _as_str(rec["delivered"])),
    )


def read_ledger(path: Path) -> list[LedgerLine]:
    """Every parseable ledger line in file order (oldest first)."""
    if not path.is_file():
        return []
    out: list[LedgerLine] = []
    with path.open(encoding="utf-8") as fh:
        for raw in fh:
            line = parse_ledger_line(raw)
            if line is not None:
                out.append(line)
    return out


# --------------------------------------------------------------------------
# snapshots
# --------------------------------------------------------------------------

def _stamp_from_name(path: Path) -> str | None:
    """`joined_20260922T235134Z` -> ISO-8601; None when there is no stamp."""
    stem = path.stem
    if "_" not in stem:
        return None
    stamp = stem.rsplit("_", 1)[-1]
    try:
        return datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=UTC).isoformat(timespec="seconds")
    except ValueError:
        return None


def latest_groups_snapshot(paths: GuiPaths) -> Path | None:
    """Freshest joined-groups snapshot on disk, or None when never fetched.

    `cache/groups.json` is only picked when it exists; `--list-groups` writes
    `groups/joined_<stamp>.json`, so both candidates are ranked by mtime to
    answer "what is the newest truth here?" without guessing a writer.
    """
    candidates: list[Path] = []
    if paths.fixed_groups.is_file():
        candidates.append(paths.fixed_groups)
    if paths.groups_dir.is_dir():
        candidates.extend(p for p in paths.groups_dir.glob("joined_*.json")
                          if p.is_file())
    if not candidates:
        return None
    # mtime first; the name breaks ties (equal mtimes are common when two
    # snapshots land in the same second) and the stamp sorts chronologically.
    return max(candidates, key=lambda p: (p.stat().st_mtime, p.name))


def read_groups(paths: GuiPaths) -> dict[str, Any]:
    """Joined-groups view: snapshot rows + fetched_at + source path."""
    snap = latest_groups_snapshot(paths)
    if snap is None:
        return {"available": False, "fetched_at": None, "source": None,
                "count": 0, "rows": []}
    try:
        data = json.loads(snap.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"available": False, "fetched_at": None, "source": str(snap),
                "count": 0, "rows": [],
                "error": f"unreadable snapshot: {snap.name}"}
    rows: list[dict[str, Any]] = []
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            gid = _as_str(item.get("id")).strip()
            url = _as_str(item.get("url"))
            if not gid or not url:
                continue
            rows.append({"id": gid, "name": _as_str(item.get("name")) or gid,
                         "url": url, "last_visited": _as_int(item.get("last_visited"))})
    fetched = _stamp_from_name(snap)
    if fetched is None:
        fetched = datetime.fromtimestamp(
            snap.stat().st_mtime, tz=UTC).isoformat(timespec="seconds")
    return {"available": True, "fetched_at": fetched, "source": str(snap),
            "count": len(rows), "rows": rows}


def read_listings(paths: GuiPaths) -> dict[str, Any]:
    """Marketplace listings view: rows + fetched_at from the snapshot mtime."""
    path = paths.listings
    if not path.is_file():
        return {"available": False, "fetched_at": None, "source": None,
                "count": 0, "rows": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"available": False, "fetched_at": None, "source": str(path),
                "count": 0, "rows": [], "error": "unreadable listings.json"}
    rows: list[dict[str, Any]] = []
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict) or not _as_str(item.get("id")):
                continue
            rows.append({
                "id": _as_str(item.get("id")),
                "title": _as_str(item.get("title")),
                "price": _as_str(item.get("price")),
                "approved": item.get("approved"),
                "rejected": item.get("rejected"),
            })
    fetched = datetime.fromtimestamp(
        path.stat().st_mtime, tz=UTC).isoformat(timespec="seconds")
    return {"available": True, "fetched_at": fetched, "source": str(path),
            "count": len(rows), "rows": rows}


# --------------------------------------------------------------------------
# rollups
# --------------------------------------------------------------------------

def group_rollup(lines: list[LedgerLine]) -> dict[str, dict[str, Any]]:
    """Per-group_id: last attempt ts/status, attempt count, published count.

    Counters come from group-kind lines only (the ledger now also carries
    crosspost lines). `last_attempt_ts` from poster.results supplies the
    rotation clock, so the dashboard and the bot cannot disagree about which
    group is next.
    """
    by_group: dict[str, dict[str, Any]] = {}
    for line in lines:
        if line.kind != KIND_GROUP:
            continue
        row = by_group.setdefault(line.group_id, {
            "group_id": line.group_id, "name": line.name, "last_ts": "",
            "last_status": "", "last_error": None, "last_run_id": "",
            "attempts": 0, "published": 0, "staged": 0, "failed": 0,
            "skipped": 0, "dry_runs": 0, "delivered": "",
        })
        row["attempts"] += 1
        if line.status in ("published", "staged", "failed", "skipped"):
            row[line.status] = row.get(line.status, 0) + 1
        if line.dry_run:
            row["dry_runs"] += 1
        if line.status == "published" and line.delivered:
            # verdict travels with the NEWEST publish (a failed later
            # attempt does not invalidate what is already visible)
            row["delivered"] = line.delivered
        if line.ts >= row["last_ts"]:
            row["last_ts"] = line.ts
            row["last_status"] = line.status
            row["last_error"] = line.error
            row["last_run_id"] = line.run_id
            if line.name:
                row["name"] = line.name
    return by_group


def crosspost_rollup(lines: list[LedgerLine]) -> dict[str, dict[str, Any]]:
    """Per-listing_id crosspost coverage gathered from batch ledger lines."""
    by_listing: dict[str, dict[str, Any]] = {}
    for line in lines:
        if line.kind != KIND_CROSSPOST:
            continue
        row = by_listing.setdefault(line.listing_id, {
            "listing_id": line.listing_id, "listing_title": line.listing_title,
            "batches": 0, "groups": [], "last_ts": "", "last_status": "",
            "last_error": None, "last_batch": None, "last_count": None,
            "delivered": "",
        })
        if line.status == "published" and line.delivered:
            # newest sampled verdict wins (mirrors group_rollup semantics)
            row["delivered"] = line.delivered
        row["batches"] += 1
        for gid in line.group_ids:
            if gid not in row["groups"]:
                row["groups"].append(gid)
        if line.ts >= row["last_ts"]:
            row["last_ts"] = line.ts
            row["last_status"] = line.status
            row["last_error"] = line.error
            row["last_batch"] = line.batch
            row["last_count"] = line.count
            if line.listing_title:
                row["listing_title"] = line.listing_title
    return by_listing


def share_rollup(lines: list[LedgerLine]) -> dict[str, dict[str, Any]]:
    """Per-listing_id share coverage from individual share rows.

    SHARE rows only: each row is ONE listing→group share, unlike a crosspost
    row which is a whole batch. The set of DISTINCT group_ids is what the
    audit card shows as "groups shared to" (a group re-shared in a later run
    must not inflate the count).
    """
    by_listing: dict[str, dict[str, Any]] = {}
    for line in lines:
        if line.kind != KIND_SHARE:
            continue
        row = by_listing.setdefault(line.listing_id, {
            "listing_id": line.listing_id, "listing_title": line.listing_title,
            "groups": [], "last_ts": "", "last_status": "", "last_error": None,
        })
        if line.group_id and line.group_id not in row["groups"]:
            row["groups"].append(line.group_id)
        if line.ts >= row["last_ts"]:
            row["last_ts"] = line.ts
            row["last_status"] = line.status
            row["last_error"] = line.error
            if line.listing_title:
                row["listing_title"] = line.listing_title
    return by_listing


def share_audit(rollup: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """The read-only SHARE audit card rows: per listing, how many distinct
    groups were shared to, the newest share's ts and status; newest first."""
    rows = [{
        "title": r["listing_title"] or r["listing_id"],
        "listing_id": r["listing_id"],
        "groups": len(r["groups"]),
        "last_ts": r["last_ts"],
        "last_status": r["last_status"],
    } for r in rollup.values()]
    rows.sort(key=lambda r: r["last_ts"], reverse=True)
    return rows


def _run_summaries(lines: list[LedgerLine]) -> list[dict[str, Any]]:
    """Recent runs, newest first: one row per run_id with its status mix."""
    runs: dict[str, dict[str, Any]] = {}
    for line in lines:
        run = runs.setdefault(line.run_id or "?", {
            "run_id": line.run_id, "last_ts": "", "dry_run": None,
            "kinds": [], "total": 0, "published": 0, "staged": 0, "failed": 0,
            "skipped": 0, "crossposts": 0,
        })
        run["total"] += 1
        if line.ts >= run["last_ts"]:
            run["last_ts"] = line.ts
            if line.dry_run is not None:
                run["dry_run"] = line.dry_run
        if line.kind not in run["kinds"]:
            run["kinds"].append(line.kind)
        if line.kind == KIND_CROSSPOST:
            run["crossposts"] += 1
        elif line.status in ("published", "staged", "failed", "skipped"):
            run[line.status] = run.get(line.status, 0) + 1
    ordered = sorted(runs.values(), key=lambda r: r["last_ts"], reverse=True)
    return ordered[:RECENT_RUN_LIMIT]


def _recent_logs(paths: GuiPaths) -> list[dict[str, Any]]:
    if not paths.logs_dir.is_dir():
        return []
    files = [p for p in paths.logs_dir.glob("run_*.log") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [{"name": p.name,
             "mtime": datetime.fromtimestamp(p.stat().st_mtime, tz=UTC).isoformat(
                 timespec="seconds"),
             "size": p.stat().st_size} for p in files[:RECENT_LOG_LIMIT]]


def rotation_view(rows: list[dict[str, Any]], last: dict[str, str],
                  cap: int) -> dict[str, Any]:
    """Which rows the NEXT run would attempt — computed with the bot's own
    `select_targets`, so the dashboard cannot invent a different order."""
    planned = select_targets([dict(r) for r in rows], last, cap) if cap > 0 else []
    planned_ids = [g["id"] for g in planned]
    never = [r["id"] for r in rows if r["id"] not in last]
    return {"cap": cap, "planned": planned_ids, "never_attempted": never,
            "attempted_count": len(rows) - len(never), "total": len(rows)}


def _name_of(rows: list[dict[str, Any]], group_id: str) -> str:
    """Group name by id, in the caller's own order (rotation order must not be
    re-sorted into FB's join order)."""
    for row in rows:
        if row["id"] == group_id:
            return str(row.get("name") or group_id)
    return group_id


def build_state(paths: GuiPaths, identity: dict[str, Any] | None = None,
                manager_status: dict[str, Any] | None = None) -> dict[str, Any]:
    """The whole dashboard payload from disk only (no browser, no subprocess)."""
    identity = identity or {}
    groups = read_groups(paths)
    listings = read_listings(paths)
    ledger_exists = paths.ledger.is_file()
    lines = read_ledger(paths.ledger)
    last = last_attempt_ts(paths.ledger)
    by_group = group_rollup(lines)
    by_listing = crosspost_rollup(lines)
    by_share = share_rollup(lines)
    # published totals are derived from group-kind lines, NOT from
    # poster.results.count_published: that helper is the group-only reader and
    # would count a published crosspost line as a phantom "unknown" group,
    # inflating the all-time totals the dashboard shows.

    for row in groups["rows"]:
        roll = by_group.get(row["id"], {})
        row["last_ts"] = roll.get("last_ts", "")
        row["last_status"] = roll.get("last_status", "never")
        row["last_error"] = roll.get("last_error")
        row["attempts"] = roll.get("attempts", 0)
        row["published"] = roll.get("published", 0)
        row["delivered"] = roll.get("delivered", "")
        # a group whose last attempt found no composer is (currently) not
        # postable for this identity — the marketplace case in AGENTS.md
        row["postable"] = row["last_status"] != STATUS_SKIPPED

    cap = int(identity.get("max_posts_per_run") or 0)
    rotation = rotation_view(groups["rows"], last, cap)
    planned = set(rotation["planned"])
    for row in groups["rows"]:
        row["next"] = row["id"] in planned
    rotation["planned_names"] = [_name_of(groups["rows"], gid)
                                for gid in rotation["planned"]]

    total_groups = len(groups["rows"])
    for row in listings["rows"]:
        cov = by_listing.get(row["id"], {})
        row["delivered"] = cov.get("delivered", "")
        row["crossposts"] = cov.get("batches", 0)
        row["crosspost_groups"] = len(cov.get("groups", []))
        row["crosspost_coverage"] = (
            round(100 * row["crosspost_groups"] / total_groups)
            if total_groups else None)
        row["last_ts"] = cov.get("last_ts", "")
        row["last_status"] = cov.get("last_status", "never")

    recent = [line.to_dict() for line in lines[-RECENT_LEDGER_LIMIT:]][::-1]
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "paths": {"capture_root": str(paths.capture_root),
                  "ledger": str(paths.ledger),
                  "listings": str(paths.listings),
                  "groups": groups.get("source") or str(paths.groups_dir)},
        "identity": identity,
        "groups": groups,
        "listings": listings,
        "rotation": rotation,
        "share": {"available": bool(by_share),
                  "rows": share_audit(by_share)},
        "ledger": {
            "available": ledger_exists, "lines": len(lines),
            "group_lines": sum(1 for x in lines if x.kind == KIND_GROUP),
            "crosspost_lines": sum(1 for x in lines if x.kind == KIND_CROSSPOST),
            "share_lines": sum(1 for x in lines if x.kind == KIND_SHARE),
            "unknown_lines": sum(1 for x in lines if x.kind == KIND_UNKNOWN),
            "status_counts": status_counts(lines),
            "published_total": sum(r["published"] for r in by_group.values()),
            "totals_published": {gid: r["published"] for gid, r in by_group.items()
                                 if r["published"]},
            "recent": recent,
        },
        "runs": {"recent": _run_summaries(lines), "logs": _recent_logs(paths)},
        "manager": manager_status or {"running": False},
    }


def status_counts(lines: list[LedgerLine]) -> dict[str, int]:
    """Ledger-wide status histogram (failed includes explicit failures only)."""
    counts = {"published": 0, "staged": 0, "failed": 0, "skipped": 0}
    for line in lines:
        if line.status in counts:
            counts[line.status] += 1
    counts["failed_or_skipped"] = counts[STATUS_FAILED] + counts[STATUS_SKIPPED]
    return counts
