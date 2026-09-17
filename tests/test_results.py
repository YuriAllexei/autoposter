"""Unit tests for the results ledger (no browser, no network)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poster.results import (
    STATUS_FAILED,
    STATUS_PUBLISHED,
    RunRecorder,
    count_published,
    group_id_from_url,
)

GROUP = {"name": "VENTAS CUAUHTEMOC", "group_url": "https://www.facebook.com/groups/249803862915566"}


def test_group_id_from_url():
    assert group_id_from_url(GROUP["group_url"]) == "249803862915566"
    assert group_id_from_url("") == "unknown"


def test_record_appends_one_jsonl_line(tmp_path):
    led = tmp_path / "ledger.jsonl"
    r = RunRecorder(ledger_path=led, run_id="R1", dry_run=False)
    r.record(GROUP, True)
    r.record(GROUP, False, "flow_error: boom")
    lines = [json.loads(x) for x in led.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2
    assert lines[0]["status"] == STATUS_PUBLISHED and lines[0]["group_id"] == "249803862915566"
    assert lines[1]["status"] == STATUS_FAILED and "boom" in lines[1]["error"]


def test_dry_run_staged_never_counts_as_published(tmp_path):
    led = tmp_path / "ledger.jsonl"
    RunRecorder(ledger_path=led, run_id="R1", dry_run=True).record(GROUP, True)
    assert count_published(led) == {}


def test_cumulative_counts_across_runs(tmp_path):
    led = tmp_path / "ledger.jsonl"
    for run in ("R1", "R2"):
        RunRecorder(ledger_path=led, run_id=run, dry_run=False).record(GROUP, True)
    assert count_published(led) == {"249803862915566": 2}


def test_malformed_ledger_lines_are_skipped(tmp_path):
    led = tmp_path / "ledger.jsonl"
    led.write_text("not json\n" + json.dumps(
        {"group_id": "7", "status": STATUS_PUBLISHED}) + "\n", encoding="utf-8")
    assert count_published(led) == {"7": 1}


def test_summary_shape(tmp_path):
    r = RunRecorder(ledger_path=tmp_path / "l.jsonl", run_id="R1", dry_run=False)
    r.record(GROUP, True)
    r.record(GROUP, False, "nope")
    s = r.summary()
    assert s["published"] == 1 and s["failed"] == 1 and s["attempted"] == 2
    assert s["totals_published_all_time"] == {"249803862915566": 1}
    assert s["groups"][1]["error"] == "nope" and s["duration_s"] >= 0
