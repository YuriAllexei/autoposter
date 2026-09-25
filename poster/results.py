"""Run results: append-only JSONL ledger + all-time published counters.

One line per attempt, stamped with the pipeline that wrote it
(`kind`: "group" | "crosspost" | "share") — every reader filters on kind, see
record_kind(). Statuses:
  published  live run, flow finished without error (Publicar clicked)
  staged     dry-run: composer was fully staged then closed — NOT a real post,
             therefore never counted in the all-time totals
  failed     flow/runner error, evidence saved under AP_SCREENSHOT_DIR
  skipped    the step was deliberately not attempted (text: composer trigger
             never rendered within the 5s gate; share: group not offerable /
             ambiguous name) — not a failure, never counted as published
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

#: `kind` discriminator on every ledger row. ONE ledger file is shared by
#: three pipelines, so every line is stamped with the pipeline that wrote it
#: and every READER filters on it — a share row carries a group_id, and an
#: unfiltered reader would let it push the TEXT rotation clock (see
#: last_attempt_ts) or inflate the all-time published counters. Rows written
#: before the field existed carry no `kind`; record_kind() infers those from
#: the ids they hold (group_id -> group, listing_id -> crosspost).
KIND_GROUP = "group"
KIND_CROSSPOST = "crosspost"
KIND_SHARE = "share"


def record_kind(rec: Mapping) -> str:
    """The pipeline a ledger row belongs to: the explicit `kind` when present,
    else inferred from its ids for rows written before the field existed."""
    kind = str(rec.get("kind") or "").strip().lower()
    if kind:
        return kind
    if str(rec.get("group_id") or "").strip():
        return KIND_GROUP
    if str(rec.get("listing_id") or "").strip():
        return KIND_CROSSPOST
    return "unknown"


def is_group_record(rec: Mapping) -> bool:
    """True only for TEXT-pipeline rows (the ones the rotation clock and the
    all-time published counters are about)."""
    return record_kind(rec) == KIND_GROUP


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
    for picking which joined groups to post next (never-attempted first).

    TEXT-pipeline rows only (Slice D): crosspost and share rows must never
    make a group look 'attempted' to the text rotation."""
    last: dict[str, str] = {}
    for rec in _iter_records(ledger_path):
        if not is_group_record(rec):
            continue
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
    """All-time published count per group_id. Corrupt lines are skipped.
    TEXT-pipeline rows only (Slice D): a published crosspost/share row must
    not inflate a group's published tally."""
    counts: dict[str, int] = {}
    for rec in _iter_records(ledger_path):
        if rec.get("status") == STATUS_PUBLISHED and is_group_record(rec):
            gid = str(rec.get("group_id") or "unknown")
            counts[gid] = counts.get(gid, 0) + 1
    return counts


def count_crossposts(ledger_path: Path) -> dict[str, int]:
    """All-time PUBLISHED batches per listing_id (dry-runs never count —
    same discipline as count_published). Reporting only: feeds the crosspost
    Discord summary's totals. No run DECISION reads ledger history —
    batch tracking is in-run only (user rule 2026-09-23).

    CROSSPOST rows only (Slice D): share rows also carry a listing_id and
    must not count as crosspost batches."""
    counts: dict[str, int] = {}
    for rec in _iter_records(ledger_path):
        if record_kind(rec) != KIND_CROSSPOST:
            continue
        lid = str(rec.get("listing_id") or "")
        if lid and rec.get("status") == STATUS_PUBLISHED:
            counts[lid] = counts.get(lid, 0) + 1
    return counts


def count_shares(ledger_path: Path) -> dict[str, int]:
    """All-time PUBLISHED share rows per listing_id (dry never counts).
    Reporting only — the share pipeline's only memory is its in-run done-set
    (user rule 2026-09-24, same as crosspost)."""
    counts: dict[str, int] = {}
    for rec in _iter_records(ledger_path):
        if record_kind(rec) != KIND_SHARE:
            continue
        lid = str(rec.get("listing_id") or "")
        if lid and rec.get("status") == STATUS_PUBLISHED:
            counts[lid] = counts.get(lid, 0) + 1
    return counts


def count_share_lines(ledger_path: Path) -> int:
    """Every share ledger row (any status) — the dashboard's 'share lines'
    stat and a cheap activity gauge for the share pipeline."""
    return sum(1 for rec in _iter_records(ledger_path)
               if record_kind(rec) == KIND_SHARE)


@dataclass
class GroupResult:
    name: str
    group_id: str
    status: str
    error: str | None = None
    #: did the post actually go LIVE? "pending" (admin queue), "live"
    #: (visible in the feed), "unknown" (verify could not tell). None on
    #: staged/failed/skipped rows — verification only applies to publishes.
    delivered: str | None = None


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
    # sampled delivery verdict, live batches only (see poster.crosspost)
    delivered: str | None = None


@dataclass
class ShareResult:
    """ONE share of one listing into one group (poster/share.py)."""
    listing_id: str
    listing_title: str
    group_id: str
    group_name: str
    status: str
    error: str | None = None
    # live publishes only: best-effort delivery verdict (never gates status)
    delivered: str | None = None


@dataclass
class RunRecorder:
    """Collects this run's outcomes and mirrors them into the ledger file."""

    ledger_path: Path
    run_id: str
    dry_run: bool
    started: datetime = field(default_factory=lambda: datetime.now(UTC))
    results: list[GroupResult] = field(default_factory=list)
    cross_results: list[CrossResult] = field(default_factory=list)
    share_results: list[ShareResult] = field(default_factory=list)

    def crosspost(self, listing: Mapping, batch: int, groups: list,
                  ok: bool, error: str | None = None,
                  delivered: str | None = None) -> CrossResult:
        """One dialog-submission for one listing (groups = [{'id','name'}...]).

        delivered: the SAMPLED verify verdict for a real publish — like the
        group pipeline's, it decorates the row and never gates it.
        """
        status = (STATUS_STAGED if self.dry_run else STATUS_PUBLISHED) \
            if ok else STATUS_FAILED
        if not ok or self.dry_run:
            delivered = None
        res = CrossResult(
            listing_id=str(listing["id"]),
            listing_title=str(listing.get("title") or listing["id"])[:120],
            batch=batch, count=len(groups),
            group_ids=[str(g["id"]) for g in groups],
            status=status, error=error, delivered=delivered)
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
            "kind": KIND_CROSSPOST,
            "run_id": self.run_id,
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "listing_id": res.listing_id, "listing_title": res.listing_title,
            "batch": res.batch, "count": res.count,
            "group_ids": res.group_ids,
            "group_names": [str(g.get("name") or "")[:80] for g in groups],
            "status": res.status, "error": res.error, "dry_run": self.dry_run,
        }
        if res.delivered:
            line["delivered"] = res.delivered
        with self.ledger_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")

    def share(self, listing: Mapping, group: Mapping, ok: bool,
              error: str | None = None,
              delivered: str | None = None) -> ShareResult:
        """ONE share of `listing` into `group` (individual share pipeline).

        `delivered` is the best-effort verify verdict of a real publish: like
        the other pipelines it decorates the row and never gates the status,
        and it is dropped for dry runs / failures."""
        if ok:
            status = STATUS_STAGED if self.dry_run else STATUS_PUBLISHED
        else:
            status = STATUS_FAILED
        if not ok or self.dry_run:
            delivered = None
        res = ShareResult(
            listing_id=str(listing["id"]),
            listing_title=str(listing.get("title") or listing["id"])[:120],
            group_id=str(group.get("id") or ""),
            group_name=str(group.get("name") or group.get("id") or "")[:80],
            status=status, error=error, delivered=delivered)
        self.share_results.append(res)
        self._append_ledger_share(res)
        return res

    def share_skipped(self, listing: Mapping, group: Mapping,
                      reason: str) -> ShareResult:
        """The share was deliberately NOT attempted (e.g. the group name is
        ambiguous in the picker, or the group is not offerable). Recorded for
        reporting; never counts as shared/published."""
        res = ShareResult(
            listing_id=str(listing["id"]),
            listing_title=str(listing.get("title") or listing["id"])[:120],
            group_id=str(group.get("id") or ""),
            group_name=str(group.get("name") or group.get("id") or "")[:80],
            status=STATUS_SKIPPED, error=reason)
        self.share_results.append(res)
        self._append_ledger_share(res)
        return res

    def _append_ledger_share(self, res: ShareResult) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        line = {
            "kind": KIND_SHARE,
            "run_id": self.run_id,
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "listing_id": res.listing_id, "listing_title": res.listing_title,
            "group_id": res.group_id, "group_name": res.group_name,
            "status": res.status, "error": res.error, "dry_run": self.dry_run,
        }
        if res.delivered:
            line["delivered"] = res.delivered
        with self.ledger_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")

    def record(self, group: dict, ok: bool, error: str | None = None,
               delivered: str | None = None) -> GroupResult:
        """delivered: verify's verdict — recorded only on real publishes
        (a staged dry run never went anywhere to verify)."""
        if ok:
            status = STATUS_STAGED if self.dry_run else STATUS_PUBLISHED
        else:
            status = STATUS_FAILED
        if not ok or self.dry_run:
            delivered = None
        name, gid = normalize_group(group)
        res = GroupResult(name=name, group_id=gid, status=status,
                          error=error, delivered=delivered)
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
            "kind": KIND_GROUP,
            "run_id": self.run_id,
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "group_id": res.group_id, "name": res.name,
            "status": res.status, "error": res.error, "dry_run": self.dry_run,
        }
        if res.delivered:
            line["delivered"] = res.delivered
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

    def summary_share(self, aborted: str | None = None) -> dict:
        """Same envelope idea as summary(), for the individual share pipeline
        (poster/share.py). `share_lines` is the all-time share-row count and
        `totals_shares_all_time` the published shares per listing."""
        return {
            "run_id": self.run_id, "dry_run": self.dry_run, "aborted": aborted,
            "started": self.started.isoformat(timespec="seconds"),
            "duration_s": round((datetime.now(UTC) - self.started).total_seconds(), 1),
            "published": sum(1 for r in self.share_results
                             if r.status == STATUS_PUBLISHED),
            "staged": sum(1 for r in self.share_results
                          if r.status == STATUS_STAGED),
            "failed": sum(1 for r in self.share_results
                          if r.status == STATUS_FAILED),
            "skipped": sum(1 for r in self.share_results
                           if r.status == STATUS_SKIPPED),
            "attempted": len(self.share_results),
            "shares": [vars(r) for r in self.share_results],
            "share_lines": count_share_lines(self.ledger_path),
            "totals_shares_all_time": count_shares(self.ledger_path),
        }
