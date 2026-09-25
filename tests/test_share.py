"""Share pipeline tests (Slice B + D).

Covered here (no browser, no network):
  * B1 config: the NEW share-gap sleep category
    (AP_SHARE_GAP_MIN/MAX_SECONDS, defaults 2.0/3.0, own ≤20s ceiling);
  * B2 results: ShareResult + RunRecorder.share() ledger rows (`kind:"share"`)
    and the share summary envelope;
  * B3 notify: the AGGREGATED Discord payload (per-listing totals + capped
    failure lines + "+N more");
  * B4 share.py: plan/done-set/--max/--group/--list/rc codes;
  * D  isolation guard: crosspost/share ledger rows never move the TEXT
    rotation clock or the all-time published counters.

The four share ritual functions live in poster/flows.py (Slice A) and are
monkeypatched through ``poster.share.fl`` so the pipeline is testable before
that slice lands — same fake-install discipline as tests/test_crosspost.py.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from poster.config import MAX_ALLOWED_SHARE_GAP, Config, load_config

REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# B1 — config: share-gap category
# ---------------------------------------------------------------------------

def _cfg(tmp_path, **kw) -> Config:
    base = {"dry_run": True, "headless": True,
            "ledger_file": tmp_path / "ledger.jsonl",
            "log_dir": tmp_path / "logs", "screenshot_dir": tmp_path / "shots",
            "profile_dir": tmp_path / "profile",
            "post_as": "profile", "fb_main_user": "61592579496197",
            "delay_min": 0.0, "delay_max": 0.01,
            "share_gap_min": 0.0, "share_gap_max": 0.01}
    base.update(kw)
    return Config(**base)


def test_share_gap_defaults_are_10_15(tmp_path):
    cfg = load_config(env_file=tmp_path / "absent.env")
    assert (cfg.share_gap_min, cfg.share_gap_max) == (10.0, 15.0)


def test_shipped_env_example_share_gap_is_10_15():
    cfg = load_config(env_file=REPO / ".env.example")
    assert (cfg.share_gap_min, cfg.share_gap_max) == (10.0, 15.0)


def test_share_gap_capped_at_20s_and_validated(tmp_path):
    env = tmp_path / ".env"
    env.write_text("AP_SHARE_GAP_MIN_SECONDS=15\n"
                   "AP_SHARE_GAP_MAX_SECONDS=900\n", encoding="utf-8")
    cfg = load_config(env_file=env)
    assert MAX_ALLOWED_SHARE_GAP == 20.0
    assert cfg.share_gap_max == MAX_ALLOWED_SHARE_GAP
    assert cfg.share_gap_min == 15.0
    with pytest.raises(ValueError, match="share-gap"):
        Config(share_gap_min=50.0, share_gap_max=10.0)


def test_share_gap_garbage_falls_back_to_defaults(tmp_path):
    env = tmp_path / ".env"
    env.write_text("AP_SHARE_GAP_MIN_SECONDS=abc\n"
                   "AP_SHARE_GAP_MAX_SECONDS=\n", encoding="utf-8")
    cfg = load_config(env_file=env)
    assert (cfg.share_gap_min, cfg.share_gap_max) == (10.0, 15.0)


# ---------------------------------------------------------------------------
# B2 — results: ShareResult + kind:"share" ledger rows + summary envelope
# ---------------------------------------------------------------------------

from poster.results import (
    KIND_CROSSPOST,
    KIND_GROUP,
    KIND_SHARE,
    RunRecorder,
    count_published,
    count_share_lines,
    count_shares,
    record_kind,
)

LISTING = {"id": "1923574311937261", "title": "2019 Chevrolet Tahoe LT"}
GROUP = {"id": "249803862915566", "name": "VENTAS CUAUHTEMOC"}


def test_share_rows_are_dry_staged_with_verdict_stripped(tmp_path):
    led = tmp_path / "ledger.jsonl"
    rec = RunRecorder(ledger_path=led, run_id="R1", dry_run=True)
    res = rec.share(LISTING, GROUP, True, delivered="pending")
    assert (res.status, res.delivered) == ("staged", None)
    line, = [json.loads(x) for x in led.read_text().splitlines()]
    assert line["kind"] == KIND_SHARE
    assert line["run_id"] == "R1" and line["dry_run"] is True
    assert line["listing_id"] == "1923574311937261"
    assert line["listing_title"] == "2019 Chevrolet Tahoe LT"
    assert line["group_id"] == "249803862915566"
    assert line["group_name"] == "VENTAS CUAUHTEMOC"
    assert line["status"] == "staged" and line["error"] is None
    assert "delivered" not in line          # dry run never verifies anything


def test_share_live_published_keeps_delivery_verdict(tmp_path):
    led = tmp_path / "ledger.jsonl"
    rec = RunRecorder(ledger_path=led, run_id="R2", dry_run=False)
    res = rec.share(LISTING, GROUP, True, delivered="pending")
    assert res.status == "published" and res.delivered == "pending"
    line, = [json.loads(x) for x in led.read_text().splitlines()]
    assert line["kind"] == KIND_SHARE and line["status"] == "published"
    assert line["delivered"] == "pending" and line["dry_run"] is False


def test_share_failed_and_skipped_record_reasons(tmp_path):
    led = tmp_path / "ledger.jsonl"
    rec = RunRecorder(ledger_path=led, run_id="R3", dry_run=False)
    bad = rec.share(LISTING, GROUP, False, error="flow_error: no row",
                    delivered="live")        # verdict dropped on failure
    skip = rec.share_skipped(LISTING, GROUP, "ambiguous duplicate name")
    assert bad.status == "failed" and bad.error == "flow_error: no row"
    assert bad.delivered is None
    assert skip.status == "skipped" and skip.error == "ambiguous duplicate name"
    lines = [json.loads(x) for x in led.read_text().splitlines()]
    assert [x["status"] for x in lines] == ["failed", "skipped"]
    assert all(x["kind"] == KIND_SHARE for x in lines)
    assert "delivered" not in lines[0]


def test_share_summary_envelope_and_counters(tmp_path):
    led = tmp_path / "ledger.jsonl"
    rec = RunRecorder(ledger_path=led, run_id="R4", dry_run=True)
    rec.share(LISTING, GROUP, True)
    rec.share(LISTING, {"id": "9", "name": "OTRO"}, True)
    rec.share(LISTING, {"id": "8", "name": "FALLA"}, False, error="boom")
    s = rec.summary_share()
    assert (s["staged"], s["failed"], s["attempted"]) == (2, 1, 3)
    assert s["dry_run"] is True and s["aborted"] is None
    assert len(s["shares"]) == 3 and s["shares"][0]["group_name"] == "VENTAS CUAUHTEMOC"
    assert all(x["kind"] == KIND_SHARE for x in
               (json.loads(ln) for ln in led.read_text().splitlines()))
    assert s["share_lines"] == 3               # all-time share rows in ledger
    assert s["totals_shares_all_time"] == {}   # dry never publishes


def test_count_helpers_are_kind_aware(tmp_path):
    led = tmp_path / "ledger.jsonl"
    led.write_text("\n".join(json.dumps(x) for x in [
        {"kind": KIND_SHARE, "listing_id": "L1", "status": "published"},
        {"kind": KIND_SHARE, "listing_id": "L1", "status": "staged"},
        {"kind": KIND_CROSSPOST, "listing_id": "L1", "status": "published"},
        {"kind": KIND_GROUP, "group_id": "G1", "status": "published"},
    ]) + "\n", encoding="utf-8")
    assert count_shares(led) == {"L1": 1}       # share published only
    assert count_share_lines(led) == 2          # every share row
    assert count_published(led) == {"G1": 1}    # group rows only


# ---------------------------------------------------------------------------
# D — isolation guard: crosspost/share rows never move the TEXT rotation
# ---------------------------------------------------------------------------

def test_share_and_crosspost_rows_do_not_move_text_rotation(tmp_path):
    """Slice D: the ledger is shared by three pipelines. A published SHARE row
    carries a group_id, so an unfiltered reader would (a) mark that group as
    'attempted' for the TEXT rotation and (b) inflate its all-time published
    count. Neither may happen."""
    from poster.main import select_targets
    from poster.results import last_attempt_ts
    led = tmp_path / "ledger.jsonl"
    led.write_text("\n".join(json.dumps(x) for x in [
        {"kind": KIND_CROSSPOST, "listing_id": "L1", "status": "published",
         "ts": "2026-09-24T10:00:00+00:00", "group_ids": ["G1"]},
        {"kind": KIND_SHARE, "listing_id": "L1", "group_id": "G1",
         "listing_title": "Tahoe", "group_name": "G1",
         "status": "published", "ts": "2026-09-24T11:00:00+00:00",
         "dry_run": False},
    ]) + "\n", encoding="utf-8")
    assert last_attempt_ts(led) == {}           # nothing counts as a text attempt
    assert count_published(led) == {}
    groups = [{"id": "G1", "name": "G1", "url": "https://x/groups/G1"},
              {"id": "G2", "name": "G2", "url": "https://x/groups/G2"}]
    # both groups still read as never-attempted -> both eligible
    assert [g["id"] for g in select_targets(groups, last_attempt_ts(led), 2)] \
        == ["G1", "G2"]


def test_legacy_rows_without_kind_still_count_as_group(tmp_path):
    """Back-compat: rows written before the `kind` field existed must keep
    rotating (inferred from the ids they carry)."""
    from poster.results import last_attempt_ts
    led = tmp_path / "ledger.jsonl"
    led.write_text(json.dumps(
        {"group_id": "G7", "status": "published",
         "ts": "2026-09-24T09:00:00+00:00"}) + "\n", encoding="utf-8")
    assert last_attempt_ts(led) == {"G7": "2026-09-24T09:00:00+00:00"}
    assert record_kind({"listing_id": "L"}) == KIND_CROSSPOST
    assert record_kind({"group_id": "G"}) == KIND_GROUP
    assert record_kind({}) == "unknown"


# ---------------------------------------------------------------------------
# B4 — poster/share.py: plan, done-set, sleeps, --max/--group, rc codes
# ---------------------------------------------------------------------------

import asyncio
from types import SimpleNamespace

from poster import share as sh
from poster.flows import FlowError
from poster.results import STATUS_FAILED, STATUS_STAGED

FEED = [{"id": "L1", "title": "Tahoe", "price": "$340.000"},
        {"id": "L2", "title": "tahoe", "price": "$1"},      # dup title -> dropped
        {"id": "L3", "title": "CR-V", "price": "$200.000"}]
GROUPS = [{"id": "g1", "name": "VENTAS CUAUHTEMOC"},
          {"id": "g2", "name": "JUAREZ AUTOS"}]
PICKER = [{"id": "g1", "name": "VENTAS CUAUHTEMOC", "rank": 0},
          {"id": "g2", "name": "JUAREZ AUTOS", "rank": 0}]


class FakeShare:
    """Stands in for the four Slice-A flow functions (monkeypatched through
    poster.share.fl so this module never depends on Slice A's internals)."""

    def __init__(self, picker=None, fail_groups=(), fail_desc=False,
                 fail_hub=False, fail_picker=False):
        self.picker = PICKER if picker is None else picker
        self.fail_groups = set(fail_groups)
        self.fail_desc = fail_desc
        self.fail_hub, self.fail_picker = fail_hub, fail_picker
        self.hubs: list[str] = []
        self.picks: list[tuple] = []
        self.staged: list[tuple] = []
        self.desc_calls: list[str] = []

    async def discover(self, page, listing, cfg, log=print):
        if getattr(self, "fail_picker", False):
            from poster.flows import FlowError
            raise FlowError("picker discovery exploded")
        return [dict(g) for g in self.picker]

    async def dismiss(self, page, cfg, log=print):
        return None

    async def open_hub(self, page, listing, cfg, log=print):
        self.hubs.append(listing["id"])
        if self.fail_hub:
            raise FlowError("hub boom")
        return [dict(p) for p in self.picker]

    async def pick(self, page, group, cfg, log=print):
        self.picks.append((group["id"], group.get("rank")))
        if str(group["id"]) in self.fail_groups:
            raise FlowError(f"share_group_row_missing: {group['id']}")

    async def stage(self, page, cfg, log=print, description="", group=None):
        self.staged.append((group["id"], description))
        return "staged"

    async def desc(self, page, cfg, log=print, listing=None):
        self.desc_calls.append(listing["id"])
        if self.fail_desc:
            raise FlowError("desc section missing")
        return f"DESC {listing['id']}"


def _install_share(monkeypatch, fake, sleeps, tmp_path):
    async def human(cfg, log, why="", lo=None, hi=None):
        sleeps.append((why, lo, hi))

    monkeypatch.setattr(sh, "human_sleep", human)
    monkeypatch.setattr(sh.fl, "open_share_hub", fake.open_hub)
    monkeypatch.setattr(sh.fl, "pick_share_group", fake.pick)
    monkeypatch.setattr(sh.fl, "stage_or_publish_share", fake.stage)
    monkeypatch.setattr(sh.fl, "fetch_listing_description", fake.desc)
    monkeypatch.setattr(sh.fl, "discover_share_groups", fake.discover)
    monkeypatch.setattr(sh.fl, "dismiss_share_dialogs", fake.dismiss)
    # never write the real .local-capture cache from a test
    monkeypatch.setattr(sh, "TARGETS_CACHE", tmp_path / "share_targets.json")


def _listing(lid="L1", title="Tahoe"):
    return {"id": lid, "title": title, "price": "$1", "url": sh.ITEM_URL.format(lid)}


class FakePage:
    """The only page surface the share loop touches."""

    def __init__(self):
        self.gotos: list[str] = []

    async def goto(self, url, **kw):
        self.gotos.append(url)


def test_run_listing_logs_the_numbered_plan_before_the_first_share(
        tmp_path, monkeypatch):
    """User 2026-09-25: the run must print EXACTLY which groups it is about
    to post to — numbered, per listing, before any share fires — so the list
    can be eyeballed (Kill run aborts)."""
    fake, sleeps = FakeShare(), []
    _install_share(monkeypatch, fake, sleeps, tmp_path)
    cfg = _cfg(tmp_path)
    rec = RunRecorder(ledger_path=cfg.ledger_file, run_id="R", dry_run=True)
    logs: list[str] = []
    n = asyncio.run(sh.run_listing(FakePage(), cfg, rec, _listing(),
                                   logs.append, {}, set(),
                                   is_last_listing=True))
    assert n == 2
    header = next(i for i, m in enumerate(logs) if "PLAN for 'Tahoe'" in m)
    assert f"{len(fake.picker)} group(s)" in logs[header]
    for i, p in enumerate(fake.picker, 1):
        line = logs[header + i]
        assert f"{i:>2}." in line and str(p["name"]) in line
    assert not any("-> " in m for m in logs[:header])   # nothing shared yet


def test_plan_log_lists_only_the_remaining_todo_groups(tmp_path, monkeypatch):
    """--group filtering and the in-run done-set both shrink the printed
    plan: what you read is exactly what gets posted."""
    fake, sleeps = FakeShare(), []
    _install_share(monkeypatch, fake, sleeps, tmp_path)
    cfg = _cfg(tmp_path)
    rec = RunRecorder(ledger_path=cfg.ledger_file, run_id="R", dry_run=True)
    logs: list[str] = []
    done = {("L1", str(fake.picker[0]["id"]))}          # first already done
    asyncio.run(sh.run_listing(FakePage(), cfg, rec, _listing(),
                               logs.append, {}, done, is_last_listing=True))
    header = next(i for i, m in enumerate(logs) if "PLAN for 'Tahoe'" in m)
    assert "1 group(s)" in logs[header]
    assert str(fake.picker[1]["name"]) in logs[header + 1]
    assert str(fake.picker[0]["name"]) not in "".join(logs[header:])


def test_run_listing_opens_a_fresh_dialog_per_share_in_plan_order(
        tmp_path, monkeypatch):
    fake, sleeps = FakeShare(), []
    _install_share(monkeypatch, fake, sleeps, tmp_path)
    cfg = _cfg(tmp_path)
    rec = RunRecorder(ledger_path=cfg.ledger_file, run_id="R", dry_run=True)
    n = asyncio.run(sh.run_listing(FakePage(), cfg, rec, _listing(), 
                                   print, {}, set(), is_last_listing=True))
    assert n == 2
    assert fake.hubs == ["L1", "L1"]              # one dialog per share
    assert fake.picks == [("g1", 0), ("g2", 0)]   # rank tagged from the picker
    assert fake.desc_calls == ["L1"]              # description cached per listing
    assert [r.status for r in rec.share_results] == [STATUS_STAGED, STATUS_STAGED]
    assert all(r.listing_id == "L1" for r in rec.share_results)


def test_share_gap_between_shares_and_never_after_the_last(tmp_path, monkeypatch):
    fake, sleeps = FakeShare(), []
    _install_share(monkeypatch, fake, sleeps, tmp_path)
    cfg = _cfg(tmp_path)                          # action 1-3s, gap 0-0.01s
    rec = RunRecorder(ledger_path=cfg.ledger_file, run_id="R", dry_run=True)
    asyncio.run(sh.run_listing(FakePage(), cfg, rec, _listing(), print,
                               {}, set(), is_last_listing=True))
    assert [s[0] for s in sleeps] == ["before share", "before next share",
                                      "before share"]
    gap = sleeps[1]
    assert (gap[1], gap[2]) == (cfg.share_gap_min, cfg.share_gap_max)
    assert sleeps[-1][0] != "before next share"   # no sleep after the last share


def test_gap_still_applies_between_listings(tmp_path, monkeypatch):
    """listing->listing is the same SHARE-GAP category: only the very last
    share of the run is unslept."""
    fake, sleeps = FakeShare(), []
    _install_share(monkeypatch, fake, sleeps, tmp_path)
    cfg = _cfg(tmp_path)
    rec = RunRecorder(ledger_path=cfg.ledger_file, run_id="R", dry_run=True)
    done: set = set()
    asyncio.run(sh.run_listing(FakePage(), cfg, rec, _listing("L1"), 
                               print, {}, done, is_last_listing=False))
    asyncio.run(sh.run_listing(FakePage(), cfg, rec, _listing("L3", "CR-V"),
                               print, {}, done, is_last_listing=True))
    gaps = [s for s in sleeps if s[0] == "before next share"]
    assert len(gaps) == 3      # L1:1+carry, L3:1; none after L3's last share


def test_failed_share_marks_pair_done_and_never_retries_in_run(
        tmp_path, monkeypatch):
    fake, sleeps = FakeShare(fail_groups={"g1"}), []
    _install_share(monkeypatch, fake, sleeps, tmp_path)
    cfg = _cfg(tmp_path)
    rec = RunRecorder(ledger_path=cfg.ledger_file, run_id="R", dry_run=True)
    done: set = set()
    asyncio.run(sh.run_listing(FakePage(), cfg, rec, _listing(), print,
                               {}, done, is_last_listing=True))
    assert [r.status for r in rec.share_results] == [STATUS_FAILED, STATUS_STAGED]
    assert done == {("L1", "g1"), ("L1", "g2")}
    # a second pass over the SAME in-run done-set shares nothing again
    n = asyncio.run(sh.run_listing(FakePage(), cfg, rec, _listing(), 
                                   print, {}, done, is_last_listing=True))
    assert n == 0 and len(fake.hubs) == 2


def test_description_failure_skips_the_listing_without_a_dialog(
        tmp_path, monkeypatch):
    fake, sleeps = FakeShare(fail_desc=True), []
    _install_share(monkeypatch, fake, sleeps, tmp_path)
    cfg = _cfg(tmp_path)
    rec = RunRecorder(ledger_path=cfg.ledger_file, run_id="R", dry_run=True)
    n = asyncio.run(sh.run_listing(FakePage(), cfg, rec, _listing(), 
                                   print, {}, set(), is_last_listing=True))
    assert n == 0 and fake.hubs == []
    assert [r.status for r in rec.share_results] == ["skipped"]  # one, synthetic


def test_share_pipeline_never_reads_the_ledger_for_decisions(
        tmp_path, monkeypatch):
    """User rule 2026-09-24: the ONLY memory is the in-run done-set. A ledger
    full of previous shares must not shrink this run's plan."""
    led = tmp_path / "ledger.jsonl"
    led.write_text("\n".join(json.dumps(
        {"kind": KIND_SHARE, "listing_id": "L1", "group_id": g["id"],
         "listing_title": "Tahoe", "group_name": g["name"], "status": "published",
         "ts": "2026-09-24T09:00:00+00:00", "dry_run": False}) for g in GROUPS)
        + "\n", encoding="utf-8")
    fake, sleeps = FakeShare(), []
    _install_share(monkeypatch, fake, sleeps, tmp_path)
    cfg = _cfg(tmp_path, ledger_file=led)
    rec = RunRecorder(ledger_path=led, run_id="R2", dry_run=True)
    n = asyncio.run(sh.run_listing(FakePage(), cfg, rec, _listing(), 
                                   print, {}, set(), is_last_listing=True))
    assert n == 2 and fake.hubs == ["L1", "L1"]


# ---- pure helpers ----

def test_normalize_listings_dedupes_by_title_and_builds_url():
    out = sh.normalize_listings(FEED)
    assert [x["id"] for x in out] == ["L1", "L3"]     # 'tahoe' dup dropped
    assert out[0]["url"] == "https://www.facebook.com/marketplace/item/L1/"


def test_filter_listings_by_term_or_id():
    out = sh.normalize_listings(FEED)
    assert [x["id"] for x in sh.filter_listings(out, ["CR-V"])] == ["L3"]
    assert [x["id"] for x in sh.filter_listings(out, ["L1"])] == ["L1"]
    assert sh.filter_listings(out, ["nope"]) == []
    assert len(sh.filter_listings(out, None)) == 2


def test_filter_groups_by_id_or_exact_name():
    assert [g["id"] for g in sh.filter_groups(GROUPS, "g2")] == ["g2"]
    assert [g["id"] for g in sh.filter_groups(GROUPS, "JUAREZ AUTOS")] == ["g2"]
    assert sh.filter_groups(GROUPS, "g9") == []
    assert len(sh.filter_groups(GROUPS, None)) == 2


def test_ranked_takes_the_picker_rank_and_falls_back_to_zero():
    picker = [{"id": "g1", "name": "VENTAS CUAUHTEMOC", "rank": 1}]
    assert sh.ranked(GROUPS[0], picker)["rank"] == 1
    assert sh.ranked(GROUPS[1], picker)["rank"] == 0     # not offered -> 0
    assert sh.ranked(GROUPS[1], None)["rank"] == 0


def test_targets_cache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(sh, "TARGETS_CACHE", tmp_path / "gui" / "share_targets.json")
    sh.write_targets_cache(_listing(), PICKER, print)
    cache = sh.read_targets_cache()
    assert cache["listing_id"] == "L1"
    assert cache["targets"] == ["VENTAS CUAUHTEMOC", "JUAREZ AUTOS"]


def test_pipeline_writes_the_targets_cache_from_the_hub(tmp_path, monkeypatch):
    fake, sleeps = FakeShare(), []
    _install_share(monkeypatch, fake, sleeps, tmp_path)
    cfg = _cfg(tmp_path)
    rec = RunRecorder(ledger_path=cfg.ledger_file, run_id="R", dry_run=True)
    asyncio.run(sh.run_listing(FakePage(), cfg, rec, _listing(), 
                               print, {}, set(), is_last_listing=True))
    assert sh.read_targets_cache()["targets"] == ["VENTAS CUAUHTEMOC",
                                                  "JUAREZ AUTOS"]


# ---- end-to-end (browser stubbed, real argparse/config/main plumbing) ----

class FakeCtx:
    async def close(self):
        return None


def _stub_entry(monkeypatch, feed=None, groups=None, state="ok"):
    async def fake_launch(cfg, p):
        return FakeCtx(), FakePage()

    async def fake_adopt(ctx, page, cfg, log):
        return state

    async def fake_fetch(page, cfg, log=None, **kw):
        return list(FEED if feed is None else feed)

    monkeypatch.setattr(sh, "launch", fake_launch)
    monkeypatch.setattr(sh, "adopt_identity", fake_adopt)
    monkeypatch.setattr(sh, "fetch_active_listings", fake_fetch)
    monkeypatch.setattr(sh, "send_summary", lambda *a, **k: True)


def _args(**kw):
    base = {"list": False, "listing": None, "max": 0, "group": None}
    base.update(kw)
    return SimpleNamespace(**base)


def _run_main_async(tmp_path, monkeypatch, *, feed=None, groups=None,
                    state="ok", fake=None, **argkw):
    fake = fake or FakeShare()
    sleeps: list = []
    _install_share(monkeypatch, fake, sleeps, tmp_path)
    _stub_entry(monkeypatch, feed=feed, groups=groups, state=state)
    cfg = _cfg(tmp_path)
    rc = asyncio.run(sh.main_async(cfg, _args(**argkw)))
    return rc, fake, cfg, sleeps


def test_main_async_happy_dry_shares_the_whole_matrix(tmp_path, monkeypatch):
    rc, fake, cfg, _ = _run_main_async(tmp_path, monkeypatch)
    assert rc == 0                                 # 2 listings x 2 groups
    assert len(fake.hubs) == 4 and len(fake.staged) == 4
    lines = [json.loads(x) for x in cfg.ledger_file.read_text().splitlines()]
    assert len(lines) == 4 and all(x["kind"] == KIND_SHARE for x in lines)
    assert all(x["status"] == STATUS_STAGED for x in lines)  # dry never publishes


def test_main_async_max_caps_listings(tmp_path, monkeypatch):
    rc, fake, _, _ = _run_main_async(tmp_path, monkeypatch, max=1)
    assert rc == 0 and len(fake.hubs) == 2         # only the first listing


def test_main_async_group_restricts_the_plan(tmp_path, monkeypatch):
    rc, fake, _, _ = _run_main_async(tmp_path, monkeypatch, group="g2")
    assert rc == 0 and [p[0] for p in fake.picks] == ["g2", "g2"]


def test_main_async_list_mode_enumerates_the_picker(tmp_path, monkeypatch):
    rc, fake, _, _ = _run_main_async(tmp_path, monkeypatch, list=True)
    assert rc == 0 and fake.hubs == [] and fake.staged == []


def test_main_async_no_active_listings_rc1(tmp_path, monkeypatch):
    rc, fake, _, _ = _run_main_async(tmp_path, monkeypatch, feed=[])
    assert rc == 1 and fake.hubs == []


def test_main_async_unmatched_listing_term_rc1(tmp_path, monkeypatch):
    rc, fake, _, _ = _run_main_async(tmp_path, monkeypatch, listing=["NOPE"])
    assert rc == 1 and fake.hubs == []


def test_main_async_picker_discovery_failure_rc1(tmp_path, monkeypatch):
    """No joins page, no shares: a dead picker skips the listing with an
    audit line and the run finishes rc1 (nothing staged)."""
    fake = FakeShare(fail_picker=True)
    rc, fake, cfg, _ = _run_main_async(tmp_path, monkeypatch, fake=fake)
    assert rc == 1
    assert fake.hubs == []
    import json as _j
    lines = [_j.loads(x) for x in cfg.ledger_file.read_text().splitlines()]
    assert [l["status"] for l in lines] == ["skipped", "skipped"]  # one/listing
    assert all("discovery" in l["error"] for l in lines)


def test_main_async_group_filter_matching_none_rc1(tmp_path, monkeypatch):
    rc, fake, _, _ = _run_main_async(tmp_path, monkeypatch, group="g99")
    assert rc == 1 and fake.hubs == []


def test_main_async_not_logged_in_rc2(tmp_path, monkeypatch):
    rc, fake, _, _ = _run_main_async(tmp_path, monkeypatch, state="login")
    assert rc == 2 and fake.hubs == []


def test_main_async_identity_failed_rc3(tmp_path, monkeypatch):
    rc, fake, _, _ = _run_main_async(tmp_path, monkeypatch, state="identity")
    assert rc == 3 and fake.hubs == []


def test_main_async_sends_one_discord_payload(tmp_path, monkeypatch):
    sent: list = []
    fake, sleeps = FakeShare(), []
    _install_share(monkeypatch, fake, sleeps, tmp_path)
    _stub_entry(monkeypatch)                       # installs its own no-op...
    monkeypatch.setattr(sh, "send_summary",        # ...so the spy goes LAST
                        lambda url, payload, log=print: sent.append(payload))
    cfg = _cfg(tmp_path)
    assert asyncio.run(sh.main_async(cfg, _args())) == 0
    assert len(sent) == 1                          # exactly ONE embed per run
    assert len(sent[0]["embeds"]) == 1
    assert "🧪 Tahoe — 2 staged" in sent[0]["embeds"][0]["description"]


def test_main_async_uncapped_matrix_120_shares(tmp_path, monkeypatch):
    """--max 0 (the default) is NO CAP: 3 listings x 40 groups = 120 shares
    (the plan's unlimited-matrix requirement)."""
    feed = [{"id": f"L{i}", "title": f"LISTING {i}", "price": "$1"}
            for i in range(3)]
    groups = [{"id": f"g{n}", "name": f"GRUPO {n}"} for n in range(40)]
    picker = [{"id": g["id"], "name": g["name"], "rank": 0} for g in groups]
    fake = FakeShare(picker=picker)
    sleeps: list = []
    _install_share(monkeypatch, fake, sleeps, tmp_path)
    _stub_entry(monkeypatch, feed=feed, groups=groups)
    cfg = _cfg(tmp_path)
    rc = asyncio.run(sh.main_async(cfg, _args(max=0)))
    assert rc == 0
    assert len(fake.staged) == 120 and len(fake.hubs) == 120
    lines = [json.loads(x) for x in cfg.ledger_file.read_text().splitlines()]
    assert len(lines) == 120 and all(x["kind"] == KIND_SHARE for x in lines)
    # exactly one share-gap between consecutive shares, none after the last
    gaps = [s for s in sleeps if s[0] == "before next share"]
    assert len(gaps) == 119


def test_cli_flags_and_defaults():
    a = sh._cli([])
    assert (a.max, a.group, a.list, a.listing, a.dry_run, a.live) == \
        (0, None, False, None, False, False)
    b = sh._cli(["--list", "--listing", "TAHOE", "--max", "3", "--group", "g1"])
    assert (b.list, b.listing, b.max, b.group) == (True, ["TAHOE"], 3,
                                                   ["g1"])


def test_cli_live_refused_without_env_authorisation(tmp_path, capsys):
    """Same fail-safe as crosspost/main: --live is refused (rc 4) unless .env
    itself says AP_DRY_RUN=false."""
    env = tmp_path / ".env"
    env.write_text("AP_DRY_RUN=true\n", encoding="utf-8")
    assert sh.main(["--live", "--env-file", str(env)]) == 4
    assert "--live refused" in capsys.readouterr().out


def test_cli_dry_flag_never_launches_a_browser(tmp_path, monkeypatch):
    """--dry-run only forces cfg.dry_run; it still routes through main_async
    (which we stub) — never a real publish path."""
    env = tmp_path / ".env"
    env.write_text("AP_DRY_RUN=false\n", encoding="utf-8")
    seen: list = []

    def spy(cfg, args):
        seen.append(cfg.dry_run)

        async def _rc():
            return 1
        return _rc()
    monkeypatch.setattr(sh, "main_async", spy)
    assert sh.main(["--dry-run", "--env-file", str(env)]) == 1
    assert seen == [True]


def test_filter_groups_accepts_a_repeated_flag_list():
    picked = sh.filter_groups(GROUPS, ["g2", "VENTAS CUAUHTEMOC"])
    assert [g["id"] for g in picked] == ["g1", "g2"]     # joined order kept
    assert sh.filter_groups(GROUPS, ["nope"]) == []
    assert len(sh.filter_groups(GROUPS, [])) == len(GROUPS)


def test_cli_group_flag_accumulates():
    ns = sh._cli(["--dry-run", "--group", "a", "--group", "b"])
    assert ns.group == ["a", "b"]
