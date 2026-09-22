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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

STATUS_PUBLISHED = "published"
STATUS_STAGED = "staged"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"


def last_attempt_ts(ledger_path: Path) -> dict[str, str]:
    """Latest ledger ts per group_id over ALL statuses — the rotation clock
    for picking which joined groups to post next (never-attempted first)."""
    last: dict[str, str] = {}
    if not ledger_path.exists():
        return last
    for raw in ledger_path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        gid = str(rec.get("group_id") or "")
        ts = str(rec.get("ts") or "")
        if gid and (ts >= last.get(gid, "")):
            last[gid] = ts
    return last


def group_id_from_url(url: str) -> str:
    m = re.search(r"/groups/(\d+)", url or "")
    return m.group(1) if m else (url or "").strip() or "unknown"


def count_published(ledger_path: Path) -> dict[str, int]:
    """All-time published count per group_id. Corrupt lines are skipped."""
    counts: dict[str, int] = {}
    if not ledger_path.exists():
        return counts
    for raw in ledger_path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if rec.get("status") == STATUS_PUBLISHED:
            gid = str(rec.get("group_id") or "unknown")
            counts[gid] = counts.get(gid, 0) + 1
    return counts


@dataclass
class GroupResult:
    name: str
    group_id: str
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

    def record(self, group: dict, ok: bool, error: str | None = None) -> GroupResult:
        if ok:
            status = STATUS_STAGED if self.dry_run else STATUS_PUBLISHED
        else:
            status = STATUS_FAILED
        res = GroupResult(
            name=str(group.get("name") or group.get("group_url") or "?"),
            group_id=group_id_from_url(str(group.get("group_url", "") or
                                            group.get("url", ""))),
            status=status, error=error)
        self.results.append(res)
        self._append_ledger(res)
        return res

    def record_skipped(self, group: dict, reason: str) -> GroupResult:
        """Composer never rendered (5s gate): group is not postable for this
        identity. Recorded for rotation, never counted as published."""
        res = GroupResult(
            name=str(group.get("name") or group.get("url") or "?"),
            group_id=str(group.get("id") or
                         group_id_from_url(str(group.get("group_url", "")))),
            status=STATUS_SKIPPED, error=reason)
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
