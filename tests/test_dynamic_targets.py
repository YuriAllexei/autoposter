"""Dynamic-target core (post groups.json era, 2026-09-22):
 - results.last_attempt_ts: rotation clock from the ledger
 - STATUS_SKIPPED: 5s composer-gate result, not a failure
 - main.select_targets: cap'd rotation over live-joined groups
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poster.main import select_targets
from poster.results import (
    STATUS_FAILED,
    STATUS_PUBLISHED,
    STATUS_SKIPPED,
    STATUS_STAGED,
    RunRecorder,
    last_attempt_ts,
)


def _g(gid, name=None):
    return {"id": gid, "name": name or f"g{gid}",
            "url": f"https://www.facebook.com/groups/{gid}/"}


# ---------- last_attempt_ts ----------

def test_last_attempt_reads_max_ts_any_status(tmp_path):
    led = tmp_path / "ledger.jsonl"
    rows = [
        {"group_id": "1", "ts": "2026-09-20T10:00:00+00:00", "status": STATUS_PUBLISHED},
        {"group_id": "1", "ts": "2026-09-21T10:00:00+00:00", "status": STATUS_FAILED},
        {"group_id": "2", "ts": "2026-09-19T10:00:00+00:00", "status": STATUS_STAGED},
        "corrupt{{{",
    ]
    led.write_text("\n".join(json.dumps(r) if isinstance(r, dict) else r
                             for r in rows), encoding="utf-8")
    ts = last_attempt_ts(led)
    assert ts["1"] == "2026-09-21T10:00:00+00:00"
    assert ts["2"] == "2026-09-19T10:00:00+00:00"
    assert "3" not in ts


def test_last_attempt_missing_file(tmp_path):
    assert last_attempt_ts(tmp_path / "nope.jsonl") == {}


# ---------- skipped status ----------

def test_record_skipped_lands_in_ledger_not_published(tmp_path):
    rec = RunRecorder(ledger_path=tmp_path / "l.jsonl", run_id="r", dry_run=False)
    res = rec.record_skipped(_g("7"), "no composer within 5s")
    assert res.status == STATUS_SKIPPED
    line = json.loads((tmp_path / "l.jsonl").read_text(encoding="utf-8").strip())
    assert line["status"] == STATUS_SKIPPED
    assert line["group_id"] == "7"


def test_skipped_never_counts_as_published(tmp_path):
    led = tmp_path / "l.jsonl"
    led.write_text(json.dumps({"group_id": "7", "status": STATUS_SKIPPED,
                               "ts": "x"}) + "\n", encoding="utf-8")
    from poster.results import count_published
    assert count_published(led) == {}


# ---------- select_targets ----------

def test_never_attempted_first_join_order_then_oldest():
    groups = [_g("a"), _g("b"), _g("c"), _g("d")]
    last = {"a": "2026-09-22T09:00:00+00:00",   # tried today
            "c": "2026-09-20T09:00:00+00:00",   # oldest
            "d": "2026-09-21T09:00:00+00:00"}   # newer than c
    # b never attempted -> first, then existing order by age: c, d, a
    picked = select_targets(groups, last, cap=3)
    assert [g["id"] for g in picked] == ["b", "c", "d"]


def test_never_attempted_stable_by_join_order():
    groups = [_g("x"), _g("y"), _g("z")]
    picked = select_targets(groups, {}, cap=2)
    assert [g["id"] for g in picked] == ["x", "y"]  # FB joins order preserved


def test_cap_zero_or_negative_returns_empty():
    assert select_targets([_g("a")], {}, cap=0) == []


def test_only_filter_by_id_or_name():
    groups = [_g("249803862915566", "Ventas"), _g("504281104001309", "Juárez")]
    assert select_targets(groups, {}, cap=3, only="504281")[0]["id"] == "504281104001309"
    assert select_targets(groups, {}, cap=3, only="Ventas")[0]["id"] == "249803862915566"
    assert select_targets(groups, {}, cap=3, only="nope") == []
