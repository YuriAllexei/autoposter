"""Crosspost pipeline tests: dialog payload parsing, ledger coverage memory,
recorder schema, Discord payload, batch orchestration, CLI exit codes.

Selectors themselves are proven live (flows.py header cites the probe); these
tests pin the DATA and CONTROL logic around them.
"""
from __future__ import annotations

import json

import pytest

from poster import crosspost as cx
from poster.config import MAX_ALLOWED_CROSSPOST_GAP, Config, load_config
from poster.flows import _norm, parse_crosspost_targets
from poster.notify import build_crosspost_payload
from poster.results import (
    STATUS_FAILED,
    STATUS_PUBLISHED,
    STATUS_SKIPPED,
    STATUS_STAGED,
    RunRecorder,
    count_crossposts,
)


def _cfg(tmp_path, **kw) -> Config:
    base = {"dry_run": True, "headless": True,
            "ledger_file": tmp_path / "ledger.jsonl",
            "log_dir": tmp_path / "logs", "screenshot_dir": tmp_path / "shots",
            "profile_dir": tmp_path / "profile",
            "post_as": "profile", "fb_main_user": "61592579496197",
            "delay_min": 0.0, "delay_max": 0.01,
            "crosspost_gap_min": 0.0, "crosspost_gap_max": 0.01}
    base.update(kw)
    return Config(**base)


def _dialog_body(groups, limit=20, listing_id="LX"):
    viewer = {"marketplace_crosspost_limit": limit,
              "marketplace_suggested_crosspost_targets": {
                  "edges": [{"node": {"__typename": "Group", "id": gid,
                                      "name": name}}
                            for gid, name in groups]},
              "marketplace_listing": {"cross_post_info": {
                  "all_listings": [{"__typename": "GroupCommerceProductItem",
                                    "origin_group": None,
                                    "id": listing_id}]}}}
    first = json.dumps({"data": {"viewer": viewer}})
    defer = json.dumps({"label": "x", "path": ["viewer"],
                        "data": {"lint": "later"}})
    return f"for(;;);{first}\n{defer}\n"


# ---------------- payload parser ----------------

def test_parse_targets_order_limit_and_quirks():
    body = _dialog_body([("g1", "Uno"), ("g2", "Dos"), ("g3", "Tres")], limit=20)
    groups, cap, lid = parse_crosspost_targets(body)
    assert [(g["id"], g["name"]) for g in groups] == [("g1", "Uno"), ("g2", "Dos"),
                                                       ("g3", "Tres")]
    assert cap == 20 and lid == "LX"


def test_parse_targets_missing_limit_defaults_20_and_garbage_nodes_drop():
    body = json.dumps({"data": {"viewer": {
        "marketplace_suggested_crosspost_targets": {"edges": [
            {"node": {"id": "a", "name": "A"}}, {"node": {"name": "no-id"}},
            {"node": None}, {"nope": 1}]}}}})
    groups, cap, lid = parse_crosspost_targets(body)
    assert groups == [{"id": "a", "name": "A"}]
    assert cap == 20 and lid == ""


def test_norm_matches_accented_folded_names():
    assert _norm("VENTAS DE CARROS CUAUHTÉMOC 💥 CHIHUAHUA") == \
        _norm("ventas de carros cuauhtémoc  💥\nchihuahua")


# ---------------- ledger helpers ----------------

def _write(tmp_path, lines):
    p = tmp_path / "ledger.jsonl"
    p.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")
    return p


def test_count_crossposts_published_batches_only(tmp_path):
    p = _write(tmp_path, [
        {"listing_id": "L1", "status": STATUS_PUBLISHED},
        {"listing_id": "L1", "status": STATUS_PUBLISHED},
        {"listing_id": "L1", "status": STATUS_STAGED},
        {"listing_id": "L2", "status": STATUS_SKIPPED},
    ])
    assert count_crossposts(p) == {"L1": 2}


def test_recorder_crosspost_line_and_summaries(tmp_path):
    cfg = _cfg(tmp_path)
    rec = RunRecorder(ledger_path=cfg.ledger_file, run_id="R1", dry_run=True)
    groups = [{"id": "g1", "name": "Uno"}, {"id": "g2", "name": "Dos"}]
    rec.crosspost({"id": "L1", "title": "Tahoe"}, 0, groups, True)
    rec.crosspost({"id": "L1", "title": "Tahoe"}, 1, [{"id": "g3", "name": "Tres"}],
                  False, error="dialog row missing")
    rec.crosspost_skipped({"id": "L2", "title": "XM5"}, "all covered")
    line = json.loads(cfg.ledger_file.read_text().splitlines()[0])
    assert line["listing_id"] == "L1" and line["group_ids"] == ["g1", "g2"]
    assert line["status"] == STATUS_STAGED and line["dry_run"] is True
    assert line["group_names"] == ["Uno", "Dos"]
    s = rec.summary_crosspost()
    assert (s["staged"], s["failed"], s["skipped"], s["attempted"]) == (1, 1, 1, 3)
    assert s["totals_crossposted_batches_all_time"] == {}   # dry never publishes


# ---------------- config guards ----------------

def test_crosspost_gap_capped_and_validated(tmp_path):
    env = tmp_path / ".env"
    env.write_text("AP_CROSSPOST_GAP_MIN_SECONDS=200\n"
                   "AP_CROSSPOST_GAP_MAX_SECONDS=900\n"
                   "AP_CROSSPOST_MAX_LISTINGS=3\n")
    cfg = load_config(env_file=env)
    assert cfg.crosspost_gap_max == MAX_ALLOWED_CROSSPOST_GAP
    assert cfg.crosspost_gap_min == 200
    assert cfg.crosspost_max_listings == 3
    with pytest.raises(ValueError, match="crosspost-gap"):
        Config(crosspost_gap_min=50.0, crosspost_gap_max=10.0)


# ---------------- discord payload ----------------

def test_crosspost_payload_dry_and_live_lines():
    summary = {"run_id": "R", "dry_run": True, "aborted": None, "started": "t",
               "duration_s": 9.0, "published": 0, "staged": 1, "failed": 1,
               "skipped": 1, "attempted": 3,
               "listings": [
                   {"listing_id": "L1", "listing_title": "Tahoe", "batch": 0,
                    "count": 20, "group_ids": [], "status": STATUS_STAGED,
                    "error": None},
                   {"listing_id": "L1", "listing_title": "Tahoe", "batch": 1,
                    "count": 5, "group_ids": [], "status": STATUS_FAILED,
                    "error": "row missing"},
                   {"listing_id": "L2", "listing_title": "XM5", "batch": -1,
                    "count": 0, "group_ids": [], "status": STATUS_SKIPPED,
                    "error": "all 25 covered"},
               ],
               "totals_crossposted_batches_all_time": {}}
    desc = build_crosspost_payload(summary, "Carmazon Alex")["embeds"][0]["description"]
    assert "🧪 Tahoe — batch 0: 20 groups checked, dialog CANCELLED" in desc
    assert "❌ Tahoe b1" in desc and "⏭️ XM5 — all 25 covered" in desc
    assert "crosspost · Carmazon Alex · DRY RUN" in \
        build_crosspost_payload(summary, "Carmazon Alex")["embeds"][0]["title"]


# ---------------- orchestration (run_listing) ----------------

import asyncio


class FakePage:
    async def goto(self, *a, **k):
        return None


class FakeDialog:
    """Stands in for open/select/finish; records what each batch contained."""

    def __init__(self, groups, limit=20, fail_on=None, listing_id="L1"):
        self.groups, self.limit, self.fail_on = groups, limit, fail_on
        self.listing_id = listing_id
        self.batches_seen: list[list[str]] = []
        self.opens = 0

    async def open(self, page, title, cfg, log=print):
        self.opens += 1
        return list(self.groups), self.limit, self.listing_id

    async def select(self, page, groups, cfg, log=print):
        self.batches_seen.append([g["id"] for g in groups])
        if self.fail_on is not None and len(self.batches_seen) > self.fail_on:
            from poster.flows import FlowError
            raise FlowError("simulated dialog failure")
        return len(groups)

    async def finish(self, page, publish, cfg, log=print):
        return "cancelled (dry)" if not publish else "published"


def _install(monkeypatch, fake: FakeDialog):
    async def fast(*_a, **_k):
        return None

    async def no_human(*_a, **_k):
        return None
    monkeypatch.setattr(cx.asyncio, "sleep", fast)
    monkeypatch.setattr(cx, "human_sleep", no_human)
    monkeypatch.setattr(cx, "open_crosspost_dialog", fake.open)
    monkeypatch.setattr(cx, "select_crosspost_groups", fake.select)
    monkeypatch.setattr(cx, "finish_crosspost_dialog", fake.finish)
    monkeypatch.setattr(cx, "dismiss_crosspost_dialog", no_human)


def test_run_listing_batches_20_then_5(tmp_path, monkeypatch):
    groups = [{"id": f"g{i}", "name": f"G{i}"} for i in range(25)]
    fake = FakeDialog(groups)
    _install(monkeypatch, fake)
    cfg = _cfg(tmp_path)
    rec = RunRecorder(ledger_path=cfg.ledger_file, run_id="R", dry_run=True)
    n = asyncio.run(cx.run_listing(FakePage(), cfg, rec, "T", print,
                                   known_id="L1"))
    assert n == 2
    assert fake.batches_seen == [[f"g{i}" for i in range(20)],
                                 [f"g{i}" for i in range(20, 25)]]
    assert [r.status for r in rec.cross_results] == [STATUS_STAGED,
                                                     STATUS_STAGED]
    assert rec.cross_results[0].listing_id == "L1"


def test_run_listing_ignores_ledger_history(tmp_path, monkeypatch):
    """USER RULE 2026-09-23: tracking is IN-RUN only — earlier batch lines
    (this morning's dry preview, yesterday's live run) must NEVER suppress a
    batch. Even with full 'coverage' in the ledger, a fresh run plans 20/5."""
    groups = [{"id": f"g{i}", "name": f"G{i}"} for i in range(25)]
    p = _write(tmp_path, [{"listing_id": "L1", "status": STATUS_PUBLISHED,
                           "group_ids": [f"g{i}" for i in range(25)]}])
    cfg = _cfg(tmp_path, ledger_file=p)
    fake = FakeDialog(groups)
    _install(monkeypatch, fake)
    rec = RunRecorder(ledger_path=p, run_id="R", dry_run=True)
    n = asyncio.run(cx.run_listing(FakePage(), cfg, rec, "T", print,
                                   known_id="L1"))
    assert n == 2
    assert fake.batches_seen == [[f"g{i}" for i in range(20)],
                                 [f"g{i}" for i in range(20, 25)]]


def test_run_listing_flow_error_records_failed_batch(tmp_path, monkeypatch):
    groups = [{"id": f"g{i}", "name": f"G{i}"} for i in range(25)]
    fake = FakeDialog(groups, fail_on=1)      # 2nd select raises
    _install(monkeypatch, fake)
    cfg = _cfg(tmp_path)
    rec = RunRecorder(ledger_path=cfg.ledger_file, run_id="R", dry_run=True)
    n = asyncio.run(cx.run_listing(FakePage(), cfg, rec, "T", print,
                                   known_id="L1"))
    assert n == 1
    assert [r.status for r in rec.cross_results] == [STATUS_STAGED,
                                                     STATUS_FAILED]


def test_run_listing_dialog_open_failure_records_and_returns(tmp_path,
                                                             monkeypatch):
    class Boom(FakeDialog):
        async def open(self, page, title, cfg, log=print):
            from poster.flows import FlowError
            raise FlowError("menu never opened (simulated)")
    _install(monkeypatch, Boom([{"id": "g1", "name": "G1"}]))
    cfg = _cfg(tmp_path)
    rec = RunRecorder(ledger_path=cfg.ledger_file, run_id="R", dry_run=True)
    n = asyncio.run(cx.run_listing(FakePage(), cfg, rec, "T", print))
    assert n == 0
    assert rec.cross_results[0].status == STATUS_FAILED
    assert rec.cross_results[0].listing_id == "T"   # no ids known yet -> title key


def test_live_run_reposts_staged_only_history(tmp_path, monkeypatch):
    """A dry 'staged' batch is NOT coverage for a live run: g1 goes out for
    real; live coverage then respects the freshly PUBLISHED batches only."""
    groups = [{"id": "g1", "name": "G1"}, {"id": "g2", "name": "G2"},
              {"id": "g3", "name": "G3"}]
    p = _write(tmp_path, [{"listing_id": "L1", "status": STATUS_STAGED,
                           "group_ids": ["g1"]}])
    cfg = _cfg(tmp_path, ledger_file=p, dry_run=False)
    fake = FakeDialog(groups, limit=2)
    _install(monkeypatch, fake)
    rec = RunRecorder(ledger_path=p, run_id="R", dry_run=False)
    n = asyncio.run(cx.run_listing(FakePage(), cfg, rec, "T", print,
                                   known_id="L1"))
    assert n == 2
    assert fake.batches_seen == [["g1", "g2"], ["g3"]]
    assert [r.status for r in rec.cross_results] == [STATUS_PUBLISHED,
                                                     STATUS_PUBLISHED]


class FakeCtx:
    async def close(self):
        return None


def _stub_entry(monkeypatch, cards, feed=()):
    async def fake_launch(cfg, p):
        return FakeCtx(), FakePage()

    async def fake_adopt(ctx, page, cfg, log):
        return "ok"

    async def fake_fetch(page, cfg, log=None, **kw):
        return list(feed)

    async def fake_cards(page, **kw):
        return list(cards)
    monkeypatch.setattr(cx, "launch", fake_launch)
    monkeypatch.setattr(cx, "adopt_identity", fake_adopt)
    monkeypatch.setattr(cx, "fetch_active_listings", fake_fetch)
    monkeypatch.setattr(cx, "listing_card_titles", fake_cards)
    monkeypatch.setattr(cx, "send_summary", lambda *a, **k: True)


FEED_TAHOE = [{"id": "L1", "title": "Tahoe", "price": "$340.000",
               "approved": True, "rejected": False}]


def test_main_async_list_mode(tmp_path, monkeypatch):
    _stub_entry(monkeypatch, ["Tahoe"], FEED_TAHOE)
    cfg = _cfg(tmp_path)

    class Args:
        list = True
        listing = None
        max = 0

    assert asyncio.run(cx.main_async(cfg, Args())) == 0


def test_main_async_no_cards_rc1(tmp_path, monkeypatch):
    _stub_entry(monkeypatch, [], FEED_TAHOE)
    cfg = _cfg(tmp_path)

    class Args:
        list = False
        max = 0
        listing: list = None

    assert asyncio.run(cx.main_async(cfg, Args())) == 1


def test_main_async_unmatched_listing_term_rc1(tmp_path, monkeypatch):
    _stub_entry(monkeypatch, ["Tahoe"], FEED_TAHOE)
    cfg = _cfg(tmp_path)

    class Args:
        list = False
        max = 0
    Args.listing = ["NOPE"]

    assert asyncio.run(cx.main_async(cfg, Args())) == 1


def test_main_async_broken_feed_still_runs_cards(tmp_path, monkeypatch):
    """2026-09-23 lesson: the graphql feed is metadata-only here — its
    failure must never abort a crosspost run; the cards carry it."""
    async def boom(page, cfg, log=None, **kw):
        raise RuntimeError("graphql 500")
    _stub_entry(monkeypatch, ["Sony XM5"])
    monkeypatch.setattr(cx, "fetch_active_listings", boom)
    groups = [{"id": f"g{i}", "name": f"G{i}"} for i in range(3)]
    fake = FakeDialog(groups, listing_id="")   # id unknown -> title key
    _install(monkeypatch, fake)
    cfg = _cfg(tmp_path)

    class Args:
        list = False
        max = 0
        listing: list = None

    rc = asyncio.run(cx.main_async(cfg, Args()))
    assert rc == 0
    assert fake.batches_seen == [["g0", "g1", "g2"]]
    line = json.loads(cfg.ledger_file.read_text(encoding="utf-8")
                      .splitlines()[0])
    assert line["listing_id"] == "Sony XM5" and line["status"] == STATUS_STAGED


def test_main_async_happy_dry_full_loop(tmp_path, monkeypatch):
    _stub_entry(monkeypatch, ["Tahoe"], FEED_TAHOE)
    groups = [{"id": f"g{i}", "name": f"G{i}"} for i in range(22)]
    fake = FakeDialog(groups)
    _install(monkeypatch, fake)
    cfg = _cfg(tmp_path)

    class Args:
        list = False
        max = 0
        listing: list = None

    rc = asyncio.run(cx.main_async(cfg, Args()))
    assert rc == 0
    lines = [json.loads(x) for x in
             cfg.ledger_file.read_text(encoding="utf-8").splitlines()]
    assert [l["status"] for l in lines] == [STATUS_STAGED, STATUS_STAGED]
    assert [len(l["group_ids"]) for l in lines] == [20, 2]




def test_cli_live_refused_without_env_authorisation(tmp_path, capsys):
    """Repo fail-safe: --live alone never publishes; .env must say false."""
    env = tmp_path / ".env"
    env.write_text("AP_DRY_RUN=true\n", encoding="utf-8")
    rc = cx.main(["--live", "--env-file", str(env)])
    assert rc == 4
    assert "refused" in capsys.readouterr().out


def test_cli_dry_flag_always_wins(tmp_path):
    env = tmp_path / ".env"
    env.write_text("AP_DRY_RUN=false\n", encoding="utf-8")
    import poster.crosspost as mod
    seen = {}

    async def spy(cfg, args):
        seen["dry"] = cfg.dry_run
        return 0
    monkey_spy = spy
    old = mod.main_async
    mod.main_async = monkey_spy
    try:
        rc = mod.main(["--dry-run", "--env-file", str(env)])
    finally:
        mod.main_async = old
    assert rc == 0 and seen["dry"] is True


def test_main_async_skips_flagged_cards(tmp_path, monkeypatch):
    """USER RULE (2026-09-23 recording notes): a card absent from the ACTIVE
    feed ('Requieren atencion') is never cross-posted — the planned set is
    cards ∩ feed, cards only as the clickable anchor."""
    feed = [{"id": "L1", "title": "Tahoe", "price": "$340.000"},
            {"id": "L1", "title": "Tahoe", "price": "$340.000"}]  # dup card
    _stub_entry(monkeypatch, ["Tahoe", "Tahoe", "Sony XM5", "Sony XM5"],
                feed)
    groups = [{"id": "g1", "name": "G1"}]
    fake = FakeDialog(groups, listing_id="L1")
    _install(monkeypatch, fake)
    cfg = _cfg(tmp_path)

    class Args:
        list = False
        max = 0
        listing: list = None

    rc = asyncio.run(cx.main_async(cfg, Args()))
    assert rc == 0
    assert fake.batches_seen == [["g1"]]
    ids = [json.loads(x)["listing_id"] for x in
           cfg.ledger_file.read_text(encoding="utf-8").splitlines()]
    assert ids == ["L1"]     # 'Sony XM5' never touched


def test_main_async_feed_without_any_card_match_aborts(tmp_path, monkeypatch):
    """Feed alive but NO card matches it (weird drift): abort rc 1 loudly
    instead of clicking flagged-only listings."""
    _stub_entry(monkeypatch, ["Sony XM5"],
                [{"id": "L1", "title": "Tahoe", "price": "$0"}])
    groups = [{"id": "g1", "name": "G1"}]
    fake = FakeDialog(groups)
    _install(monkeypatch, fake)
    cfg = _cfg(tmp_path)

    class Args:
        list = False
        max = 0
        listing: list = None

    rc = asyncio.run(cx.main_async(cfg, Args()))
    assert rc == 1
    assert fake.opens == 0   # no dialog attempted


def test_crosspost_action_sleep_range(tmp_path):
    env = tmp_path / ".env"
    env.write_text("AP_CROSSPOST_ACTION_MIN_SECONDS=abc\n"
                   "AP_CROSSPOST_ACTION_MAX_SECONDS=99\n", encoding="utf-8")
    cfg = load_config(env_file=env)
    assert cfg.crosspost_action_min == 1.0      # garbage -> default
    assert cfg.crosspost_action_max == 7.0      # capped like every wait
    bad = tmp_path / "bad.env"
    bad.write_text("AP_CROSSPOST_ACTION_MIN_SECONDS=5\n"
                   "AP_CROSSPOST_ACTION_MAX_SECONDS=2\n", encoding="utf-8")
    try:
        load_config(env_file=bad)
        raise AssertionError("min>max must raise")
    except ValueError:
        pass


def test_select_rows_micro_pauses_between_clicks(tmp_path, monkeypatch):
    """USER SPEC: consecutive checkbox ticks sleep 0.2-1s BETWEEN clicks;
    the FIRST click is immediate."""
    import poster.flows as fl

    class Row:
        def __init__(self, name): self.name = name; self.checked = False
        async def inner_text(self): return self.name + "\n10 miembros\n · Público"
        async def click(self): self.checked = True
        async def get_attribute(self, a):
            return "true" if self.checked else "false"

    class Rows:
        def __init__(self, names): self.items = [Row(n) for n in names]
        async def count(self): return len(self.items)
        def nth(self, i): return self.items[i]

    class Pg:
        def __init__(self, rows): self._rows = rows
        def locator(self, sel):
            assert sel == fl.CROSSPOST_ROW_SEL   # pinned selector respected
            return self._rows

    pg = Pg(Rows([f"G{i}" for i in range(4)]))
    slept = []
    async def fake_sleep(s): slept.append(s)
    monkeypatch.setattr(fl.asyncio, "sleep", fake_sleep)
    cfg = _cfg(tmp_path)
    n = asyncio.run(fl.select_crosspost_groups(
        pg, [{"id": str(i), "name": f"G{i}"} for i in range(4)], cfg,
        lambda *_a, **_k: None))
    assert n == 4
    assert len(slept) == 3                     # BETWEEN ticks only, none first
    assert all(0.2 <= s <= 1.0 for s in slept)


# ---- sampled delivery verdict on live batches ----

def test_live_batch_records_sampled_delivery_verdict(tmp_path, monkeypatch):
    import json
    groups = [{"id": "g1", "name": "G1"}, {"id": "g2", "name": "G2"}]
    fake = FakeDialog(groups)
    _install(monkeypatch, fake)
    seen = {}

    async def fake_verify(page, url, cfg, log, snippet=""):
        seen["url"] = url
        seen["snippet"] = snippet
        return "pending"
    monkeypatch.setattr(cx, "verify_pending", fake_verify)
    cfg = _cfg(tmp_path, dry_run=False)
    rec = RunRecorder(ledger_path=cfg.ledger_file, run_id="R", dry_run=False)
    n = asyncio.run(cx.run_listing(FakePage(), cfg, rec,
                                   "2019 Chevrolet TAHOE LT", print,
                                   known_id="L1"))
    assert n == 1
    assert rec.cross_results[0].status == STATUS_PUBLISHED
    assert rec.cross_results[0].delivered == "pending"
    # sampled = FIRST group of the batch; marker = folded listing title
    assert seen["url"] == "https://www.facebook.com/groups/g1/"
    assert seen["snippet"] == "2019 chevrolet tahoe lt"
    row = [json.loads(x) for x in
           cfg.ledger_file.read_text().splitlines()][-1]
    assert row["delivered"] == "pending"


def test_dry_batch_never_carries_a_verdict(tmp_path, monkeypatch):
    fake = FakeDialog([{"id": "g1", "name": "G1"}])
    _install(monkeypatch, fake)

    async def boom(*_a, **_k):
        raise AssertionError("verify must not run on dry batches")
    monkeypatch.setattr(cx, "verify_pending", boom)
    cfg = _cfg(tmp_path)                      # dry_run=True
    rec = RunRecorder(ledger_path=cfg.ledger_file, run_id="R", dry_run=True)
    n = asyncio.run(cx.run_listing(FakePage(), cfg, rec, "T", print,
                                   known_id="L1"))
    assert n == 1
    assert rec.cross_results[0].delivered is None


def test_crashing_verdict_degrades_to_unknown_not_failure(tmp_path,
                                                          monkeypatch):
    fake = FakeDialog([{"id": "g1", "name": "G1"}])
    _install(monkeypatch, fake)

    async def crash(*_a, **_k):
        raise RuntimeError("page died mid-verify")
    monkeypatch.setattr(cx, "verify_pending", crash)
    cfg = _cfg(tmp_path, dry_run=False)
    rec = RunRecorder(ledger_path=cfg.ledger_file, run_id="R", dry_run=False)
    n = asyncio.run(cx.run_listing(FakePage(), cfg, rec, "T", print,
                                   known_id="L1"))
    assert n == 1                            # batch still counts as done
    assert rec.cross_results[0].status == STATUS_PUBLISHED
    assert rec.cross_results[0].delivered == "unknown"


# ---- _find_more_button: the 220814Z hydration-race regression ----

def _mfl():
    import poster.flows as fl
    return fl


class _Btn:
    """Locator stub: behavior driven by a spec dict."""

    def __init__(self, spec, sel, idx=None):
        self.spec, self.sel, self.idx = spec, sel, idx

    @property
    def first(self):
        return self

    def nth(self, i):
        return _Btn(self.spec, self.sel, i)

    async def wait_for(self, state=None, timeout=None):
        self.spec.setdefault("waits", []).append(timeout)
        if not self.spec.get("attaches", True):
            raise _mfl().PWTimeoutError("waited too long")

    async def count(self):
        return self.spec["counts"].get(self.sel, 0)

    async def get_attribute(self, _a):
        if "^=" in self.sel:
            return self.spec["prefix_labels"][self.idx]
        return self.spec.get("exact_label", "")


class _MorePage:
    def __init__(self, spec):
        self.spec = spec

    def locator(self, sel):
        return _Btn(self.spec, sel)


def _find_more(tmp_path, monkeypatch, spec):
    fl = _mfl()
    async def no_shot(*_a, **_k):
        return "evidence.png"
    monkeypatch.setattr(fl, "dump_evidence", no_shot)
    cfg = _cfg(tmp_path)
    page = _MorePage(spec)
    return asyncio.run(fl._find_more_button(page, "2019 Chevrolet Tahoe LT",
                                            cfg, lambda *_a: None))


def test_more_button_waits_for_hydration_then_finds_it(tmp_path, monkeypatch):
    exact = '[role="button"][aria-label="Más opciones para 2019 Chevrolet Tahoe LT"]'
    spec = {"counts": {exact: 1}, "attaches": True}
    btn = _find_more(tmp_path, monkeypatch, spec)
    assert btn.sel == exact
    assert spec["waits"][0] >= 15000     # a REAL condition-wait, not a blink


def test_more_button_rescue_scan_survives_label_drift(tmp_path, monkeypatch):
    fl = _mfl()
    exact = f'[role="button"][aria-label="{fl.CROSSPOST_MORE_PREFIX}2019 Chevrolet Tahoe LT"]'
    prefix = f'[role="button"][aria-label^="{fl.CROSSPOST_MORE_PREFIX}"]'
    # drift INSIDE the title part only (case + doubled spaces) — that is
    # what the folded rescue scan is for
    spec = {"counts": {exact: 0, prefix: 1}, "attaches": False,
            "prefix_labels": ["Más opciones para 2019 CHEVROLET  TAHOE LT"]}
    btn = _find_more(tmp_path, monkeypatch, spec)   # folded rescue hit
    assert btn.sel == prefix and btn.idx == 0


def test_more_button_absent_raises_with_evidence(tmp_path, monkeypatch):
    fl = _mfl()
    exact = f'[role="button"][aria-label="{fl.CROSSPOST_MORE_PREFIX}2019 Chevrolet Tahoe LT"]'
    prefix = f'[role="button"][aria-label^="{fl.CROSSPOST_MORE_PREFIX}"]'
    spec = {"counts": {exact: 0, prefix: 0}, "attaches": False,
            "prefix_labels": []}
    try:
        _find_more(tmp_path, monkeypatch, spec)
        raise AssertionError("must raise FlowError")
    except fl.FlowError as e:
        assert "0 folded matches" in str(e) and "evidence.png" in str(e)
