"""Run results: append-only JSONL ledger + all-time published counters.

One line per group attempt. Statuses:
  published  live run, flow finished without error (Publicar clicked)
  staged     dry-run: composer was fully staged then closed — NOT a real post,
             therefore never counted in the all-time totals
  failed     flow/runner error, evidence saved under AP_SCREENSHOT_DIR
  skipped    group entered the run but the composer trigger never rendered
             (5s gate, user rule 2026-09-22) — posting not enabled there for
             our identity; not a failure and never counted as published
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

STATUS_PUBLISHED = "published"
STATUS_STAGED = "staged"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"


def _iter_records(ledger_path: Path):
    """Parsed ledger dicts, streamed line-by-line; blank/corrupt skipped.
    Shared by every ledger reader (rotation clock, counters)."""
    if not ledger_path.exists():
        return
    with ledger_path.open(encoding="utf-8") as fh:
        for raw in fh:
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                yield rec


def last_attempt_ts(ledger_path: Path) -> dict[str, str]:
    """Latest ledger ts per group_id over ALL statuses — the rotation clock
    for picking which joined groups to post next (never-attempted first)."""
    last: dict[str, str] = {}
    for rec in _iter_records(ledger_path):
        gid = str(rec.get("group_id") or "")
        ts = str(rec.get("ts") or "")
        if gid and (ts >= last.get(gid, "")):
            last[gid] = ts
    return last


def group_id_from_url(url: str) -> str:
    m = re.search(r"/groups/(\d+)", url or "")
    return m.group(1) if m else (url or "").strip() or "unknown"


def normalize_group(group: dict) -> tuple[str, str]:
    """(name, group_id) for BOTH record shapes: live-joined dicts carry
    id/url (groups_fetch.JoinedGroup); legacy/manual ones carry group_url."""
    url = str(group.get("url") or group.get("group_url") or "")
    gid = str(group.get("id") or group_id_from_url(url))
    return str(group.get("name") or url or "?"), gid


def select_targets(groups: list, last_attempt: dict[str, str],
                   cap: int, only: str | None = None) -> list:
    """Rotation over live-joined groups: never-attempted first (preserving
    FB's joins order), then least-recently-attempted. Cap = max posts per
    run; skipped/no-composer attempts still rotate (a group may gain the
    composer later, the 5s gate re-checks cheaply)."""
    if cap <= 0:
        return []
    pool = list(groups)
    if only:
        pool = [g for g in pool if only in g["url"] or only == g["name"]
                or only == g["id"]]
    # stable sort: key = (has-been-attempted?, last ts) — ISO-8601 sorts right
    pool.sort(key=lambda g: (1, last_attempt.get(g["id"], ""))
              if g["id"] in last_attempt else (0, ""))
    return pool[:cap]


def count_published(ledger_path: Path) -> dict[str, int]:
    """All-time published count per group_id. Corrupt lines are skipped."""
    counts: dict[str, int] = {}
    for rec in _iter_records(ledger_path):
        if rec.get("status") == STATUS_PUBLISHED:
            gid = str(rec.get("group_id") or "unknown")
            counts[gid] = counts.get(gid, 0) + 1
    return counts


def count_crossposts(ledger_path: Path) -> dict[str, int]:
    """All-time PUBLISHED batches per listing_id (dry-runs never count —
    same discipline as count_published). Reporting only: feeds the crosspost
    Discord summary's totals. No run DECISION reads ledger history —
    batch tracking is in-run only (user rule 2026-09-23)."""
    counts: dict[str, int] = {}
    for rec in _iter_records(ledger_path):
        lid = str(rec.get("listing_id") or "")
        if lid and rec.get("status") == STATUS_PUBLISHED:
            counts[lid] = counts.get(lid, 0) + 1
    return counts


@dataclass
class GroupResult:
    name: str
    group_id: str
    status: str
    error: str | None = None


@dataclass
class CrossResult:
    """One crosspost BATCH (≤20 groups) of one listing."""
    listing_id: str
    listing_title: str
    batch: int
    count: int
    group_ids: list
    status: str
    error: str | None = None


@dataclass
class RunRecorder:
    """Collects this run's outcomes and mirrors them into the ledger file."""

    ledger_path: Path
    run_id: str
    dry_run: bool
    started: datetime = field(default_factory=lambda: datetime.now(UTC))
    results: list[GroupResult] = field(default_factory=list)
    cross_results: list[CrossResult] = field(default_factory=list)

    def crosspost(self, listing: Mapping, batch: int, groups: list,
                  ok: bool, error: str | None = None) -> CrossResult:
        """One dialog-submission for one listing (groups = [{'id','name'}...])."""
        status = (STATUS_STAGED if self.dry_run else STATUS_PUBLISHED) \
            if ok else STATUS_FAILED
        res = CrossResult(
            listing_id=str(listing["id"]),
            listing_title=str(listing.get("title") or listing["id"])[:120],
            batch=batch, count=len(groups),
            group_ids=[str(g["id"]) for g in groups],
            status=status, error=error)
        self.cross_results.append(res)
        self._append_ledger_cross(res, groups)
        return res

    def crosspost_skipped(self, listing: Mapping, reason: str) -> CrossResult:
        """Every offered group already covered for this listing — nothing to
        do. Recorded for rotation/reporting; never counts as published."""
        res = CrossResult(
            listing_id=str(listing["id"]),
            listing_title=str(listing.get("title") or listing["id"])[:120],
            batch=-1, count=0, group_ids=[],
            status=STATUS_SKIPPED, error=reason)
        self.cross_results.append(res)
        self._append_ledger_cross(res, [])
        return res

    def _append_ledger_cross(self, res: CrossResult, groups: list) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        line = {
            "run_id": self.run_id,
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "listing_id": res.listing_id, "listing_title": res.listing_title,
            "batch": res.batch, "count": res.count,
            "group_ids": res.group_ids,
            "group_names": [str(g.get("name") or "")[:80] for g in groups],
            "status": res.status, "error": res.error, "dry_run": self.dry_run,
        }
        with self.ledger_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")

    def record(self, group: dict, ok: bool, error: str | None = None) -> GroupResult:
        if ok:
            status = STATUS_STAGED if self.dry_run else STATUS_PUBLISHED
        else:
            status = STATUS_FAILED
        name, gid = normalize_group(group)
        res = GroupResult(name=name, group_id=gid, status=status, error=error)
        self.results.append(res)
        self._append_ledger(res)
        return res

    def record_skipped(self, group: dict, reason: str) -> GroupResult:
        """Composer never rendered (5s gate): group is not postable for this
        identity. Recorded for rotation, never counted as published."""
        name, gid = normalize_group(group)
        res = GroupResult(name=name, group_id=gid, status=STATUS_SKIPPED,
                          error=reason)
        self.results.append(res)
        self._append_ledger(res)
        return res

    def _append_ledger(self, res: GroupResult) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        line = {
            "run_id": self.run_id,
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "group_id": res.group_id, "name": res.name,
            "status": res.status, "error": res.error, "dry_run": self.dry_run,
        }
        with self.ledger_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")

    def cumulative_published(self) -> dict[str, int]:
        # deliberately RE-reads the ledger (not reused from rotation):
        # by summary time it must include THIS run's own appends.
        return count_published(self.ledger_path)

    def summary(self, aborted: str | None = None) -> dict:
        totals = self.cumulative_published()
        return {
            "run_id": self.run_id, "dry_run": self.dry_run, "aborted": aborted,
            "started": self.started.isoformat(timespec="seconds"),
            "duration_s": round((datetime.now(UTC) - self.started).total_seconds(), 1),
            "published": sum(1 for r in self.results if r.status == STATUS_PUBLISHED),
            "staged": sum(1 for r in self.results if r.status == STATUS_STAGED),
            "failed": sum(1 for r in self.results if r.status == STATUS_FAILED),
            "skipped": sum(1 for r in self.results if r.status == STATUS_SKIPPED),
            "attempted": len(self.results),
            "groups": [vars(r) for r in self.results],
            "totals_published_all_time": totals,
        }

    def summary_crosspost(self, aborted: str | None = None) -> dict:
        """Same envelope idea as summary(), for the crosspost pipeline."""
        totals = count_crossposts(self.ledger_path)
        return {
            "run_id": self.run_id, "dry_run": self.dry_run, "aborted": aborted,
            "started": self.started.isoformat(timespec="seconds"),
            "duration_s": round((datetime.now(UTC) - self.started).total_seconds(), 1),
            "published": sum(1 for r in self.cross_results if r.status == STATUS_PUBLISHED),
            "staged": sum(1 for r in self.cross_results if r.status == STATUS_STAGED),
            "failed": sum(1 for r in self.cross_results if r.status == STATUS_FAILED),
            "skipped": sum(1 for r in self.cross_results if r.status == STATUS_SKIPPED),
            "attempted": len(self.cross_results),
            "listings": [vars(r) for r in self.cross_results],
            "totals_crossposted_batches_all_time": totals,
        }
