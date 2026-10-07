"""Unit tests for the Discord notifier (payload build; network mocked)."""
import json
import signal
import subprocess
import sys
import urllib.error
from pathlib import Path

from poster import notify
from poster.notify import EMBED_DESC_LIMIT, build_payload

REPO = Path(__file__).resolve().parents[1]


def _summary(**over):
    base = {"run_id": "R1", "dry_run": False, "aborted": None,
            "started": "2026-09-17T08:00:00+00:00", "duration_s": 42.0,
            "published": 1, "staged": 0, "failed": 1, "attempted": 2,
            "groups": [
                {"name": "OK GROUP", "group_id": "1", "status": "published", "error": None},
                {"name": "BAD GROUP", "group_id": "2", "status": "failed", "error": "flow_error: x"},
            ],
            "totals_published_all_time": {"1": 7, "2": 0}}
    return {**base, **over}


def test_payload_green_when_all_published():
    p = build_payload(_summary(failed=0, attempted=1,
                               groups=[_summary()["groups"][0]]))
    e = p["embeds"][0]
    assert e["color"] == 0x2ECC71
    assert "OK GROUP" in e["description"] and "all-time published: 7" in e["description"]


def test_payload_red_on_failure_lists_reason():
    e = build_payload(_summary())["embeds"][0]
    assert e["color"] == 0xE74C3C
    assert "✅ OK GROUP" in e["description"]
    assert "❌ BAD GROUP" in e["description"] and "flow_error: x" in e["description"]


def test_payload_amber_on_dry_run():
    e = build_payload(_summary(dry_run=True, published=0, staged=2, failed=0))["embeds"][0]
    assert e["color"] == 0xF1C40F and "DRY RUN" in e["title"]


def test_payload_abort_shows_reason():
    e = build_payload(_summary(aborted="login timeout", published=0,
                               failed=0, attempted=0, groups=[]))["embeds"][0]
    assert "login timeout" in e["description"] and e["color"] == 0xE74C3C


def test_payload_truncates_to_embed_limit():
    s = _summary()
    s["groups"] = s["groups"] * 300
    s["attempted"] = len(s["groups"])
    e = build_payload(s)["embeds"][0]
    assert len(e["description"]) <= EMBED_DESC_LIMIT


def test_payload_is_json_serializable():
    json.dumps(build_payload(_summary()))


def _fake_urlopen(status_code):
    class Resp:
        status = status_code  # class bodies do not close over function locals
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b""
    return lambda *a, **k: Resp()


def test_send_ok_on_204(monkeypatch, capsys):
    monkeypatch.setattr(notify.urllib.request, "urlopen", _fake_urlopen(204))
    assert notify.send_summary("https://x/y", {"embeds": []}) is True


def test_send_retries_once_then_succeeds(monkeypatch):
    calls = []
    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise urllib.error.URLError("boom")
        class Resp:
            status = 204
            def __enter__(self): return self
            def __exit__(self, *x): return False
            def read(self): return b""
        return Resp()
    monkeypatch.setattr(notify.urllib.request, "urlopen", flaky)
    monkeypatch.setattr(notify.time, "sleep", lambda s: None)
    assert notify.send_summary("https://x/y", {}) is True
    assert len(calls) == 2


def test_send_bad_webhook_4xx_no_retry(monkeypatch):
    calls = []
    def raise404(*a, **k):
        calls.append(1)
        raise urllib.error.HTTPError("u", 404, "not found", {}, None)
    monkeypatch.setattr(notify.urllib.request, "urlopen", raise404)
    assert notify.send_summary("https://x/y", {}) is False
    assert len(calls) == 1


def test_send_never_raises(monkeypatch):
    def explode(*a, **k):
        raise OSError("no network")
    monkeypatch.setattr(notify.urllib.request, "urlopen", explode)
    monkeypatch.setattr(notify.time, "sleep", lambda s: None)
    assert notify.send_summary("https://x/y", {}) is False  # False, NOT an exception

def test_crosspost_payload_shows_verdict_on_batch_lines():
    from poster.notify import build_crosspost_payload
    summary = {
        "run_id": "R", "dry_run": False, "aborted": None,
        "started": "2026-09-23T10:00:00+00:00", "duration_s": 3.0,
        "published": 1, "staged": 0, "failed": 0, "skipped": 0,
        "attempted": 1,
        "listings": [{"listing_id": "L1", "listing_title": "Tahoe",
                      "batch": 0, "count": 20, "group_ids": [],
                      "status": "published", "error": None,
                      "delivered": "pending"}],
        "totals_crossposted_batches_all_time": {"L1": 2},
    }
    desc = build_crosspost_payload(summary)["embeds"][0]["description"]
    assert "✅ Tahoe — batch 0: 20 groups · ⏳ pending admin review" in desc
    assert "all-time batches: 2" in desc


# ---- SHARE payload: AGGREGATED (one line per listing, capped failure list) ----

def _share_row(lid, title, gid, status="staged", error=None, delivered=None):
    return {"listing_id": lid, "listing_title": title, "group_id": gid,
            "group_name": f"GRUPO {gid}", "status": status, "error": error,
            "delivered": delivered}


def _share_summary(**over):
    """3 listings x 40 groups = the 120-share matrix, all staged."""
    listings = [("L1", "2019 Chevrolet Tahoe LT"),
                ("L2", "FORD F150 XLT 2018"),
                ("L3", "Honda CR-V 2020")]
    rows = [_share_row(lid, title, str(n), "staged")
            for lid, title in listings for n in range(40)]
    base = {"run_id": "S1", "dry_run": True, "aborted": None,
            "started": "2026-09-24T12:00:00+00:00", "duration_s": 300.0,
            "published": 0, "staged": len(rows), "failed": 0, "skipped": 0,
            "attempted": len(rows), "shares": rows, "share_lines": len(rows),
            "totals_shares_all_time": {}}
    return {**base, **over}


def test_share_payload_aggregates_120_rows_into_one_embed():
    from poster.notify import build_share_payload
    p = build_share_payload(_share_summary())
    assert len(p["embeds"]) == 1                     # never one line per group
    desc = p["embeds"][0]["description"]
    assert len(desc.splitlines()) <= 14              # 120 shares, few lines
    assert "🧪 2019 Chevrolet Tahoe LT — 40 staged" in desc
    assert "🧪 Honda CR-V 2020 — 40 staged" in desc
    assert "Shares staged (dry run): 120/120" in desc
    assert p["embeds"][0]["color"] == 0xF1C40F       # amber = dry
    json.dumps(p)


def test_share_payload_caps_failure_lines_and_counts_the_rest():
    from poster.notify import build_share_payload
    rows = [_share_row("L1", "Tahoe", str(n), "failed", "flow_error: boom")
            for n in range(25)]
    s = _share_summary(shares=rows, staged=0, failed=25, attempted=25,
                       dry_run=False)
    desc = build_share_payload(s)["embeds"][0]["description"]
    assert desc.count("❌") == 10                     # capped at 10 details
    assert "+ 15 more" in desc                       # overflow summarised
    assert len(desc.splitlines()) <= 14


def test_share_payload_live_shows_published_and_totals():
    from poster.notify import build_share_payload
    rows = [_share_row("L1", "Tahoe", "1", "published", delivered="pending"),
            _share_row("L1", "Tahoe", "2", "published", delivered="live"),
            _share_row("L1", "Tahoe", "3", "skipped", "not offerable")]
    s = _share_summary(shares=rows, dry_run=False, staged=0, published=2,
                       skipped=1, attempted=3)
    s["totals_shares_all_time"] = {"L1": 7}
    e = build_share_payload(s)["embeds"][0]
    assert e["color"] == 0x2ECC71                    # green = live, no failure
    assert "✅ Tahoe — 2 published" in e["description"]
    assert "⏭️" in e["description"]                   # skip is surfaced
    assert "all-time shares: 7" in e["description"]
    assert "DRY RUN" not in e["footer"]["text"]


def test_share_payload_abort_and_dry_footer():
    from poster.notify import build_share_payload
    s = _share_summary(aborted="joined-groups fetch failed", shares=[],
                       staged=0, attempted=0)
    e = build_share_payload(s)["embeds"][0]
    assert "**⛔ Run aborted:** joined-groups fetch failed" in e["description"]
    assert e["color"] == 0xE74C3C
    assert "DRY RUN" in e["footer"]["text"]


def test_share_payload_names_every_group_on_a_single_car_run():
    """The dashboard's per-listing button runs poster.share for ONE car; the
    aggregate line alone would hide which groups it reached, so a one-listing
    run reports per group exactly like the text pipeline's embed does."""
    from poster.notify import build_share_payload
    rows = [_share_row("L1", "2017 Nissan Rogue", "1", "published",
                       delivered="pending"),
            _share_row("L1", "2017 Nissan Rogue", "2", "published",
                       delivered="live"),
            _share_row("L1", "2017 Nissan Rogue", "3", "failed",
                       "flow_error: composer never opened")]
    s = _share_summary(shares=rows, dry_run=False, published=2, staged=0,
                       failed=1, attempted=3)
    desc = build_share_payload(s)["embeds"][0]["description"]
    assert "✅ 2017 Nissan Rogue — 2 published" in desc      # headline stays
    assert "✅ GRUPO 1 · ⏳ pending admin review" in desc
    assert "✅ GRUPO 2 · 🟢 visible" in desc
    assert "❌ GRUPO 3 — `flow_error: composer never opened`" in desc
    assert desc.count("GRUPO 3") == 1        # not repeated by the failure block


def test_share_payload_caps_the_group_lines_of_a_single_car_run():
    from poster.notify import MAX_SHARE_DETAIL_LINES, build_share_payload
    rows = [_share_row("L1", "Rogue", str(n), "staged") for n in range(40)]
    s = _share_summary(shares=rows, dry_run=True, staged=40, attempted=40)
    desc = build_share_payload(s)["embeds"][0]["description"]
    assert f"+ {40 - MAX_SHARE_DETAIL_LINES} more" in desc
    assert desc.count("🧪 GRUPO") == MAX_SHARE_DETAIL_LINES


def test_share_payload_never_explodes_for_multi_car_runs():
    from poster.notify import build_share_payload
    desc = build_share_payload(_share_summary())["embeds"][0]["description"]
    assert "GRUPO 0" not in desc      # 3 listings x 40 groups stays aggregate


# --------------------------------------------------------------------------
# a run KILLED from the dashboard still reports (rule 7: aborts notify too)
# --------------------------------------------------------------------------


def test_sigterm_runs_the_abort_notice_then_dies_by_the_signal():
    """The dashboard's Kill button SIGTERMs the child's whole process group;
    the run must still deliver its ONE summary instead of dying silently — and
    it must actually DIE (a hang there would hold the dashboard's lock)."""
    code = (
        "from poster.notify import install_sigterm_notify\n"
        "install_sigterm_notify(lambda why: print('ABORT:', why, flush=True))\n"
        "import os, signal\n"
        "os.kill(os.getpid(), signal.SIGTERM)\n"
        "print('NOT REACHED', flush=True)\n"
    )
    p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, cwd=str(REPO), check=False, timeout=30)
    assert p.returncode == -signal.SIGTERM             # died BY the signal
    assert "ABORT: killed (SIGTERM)" in p.stdout
    assert "NOT REACHED" not in p.stdout               # the handler never returns


def test_sigterm_notifier_is_a_noop_off_the_main_thread():
    import threading

    from poster.notify import install_sigterm_notify
    seen: list[bool] = []
    t = threading.Thread(target=lambda: seen.append(
        install_sigterm_notify(lambda _why: None)))
    t.start()
    t.join()
    assert seen == [False]                          # no crash, no handler


def test_pipelines_wire_sigterm_to_their_latched_finish():
    """Both closing pipelines own a LATCHED finish() (one embed per run), so
    routing SIGTERM through it cannot double-post. Pinned here because the
    wiring is a single line a refactor could silently drop."""
    for name in ("share.py", "main.py"):
        src = (REPO / "poster" / name).read_text(encoding="utf-8")
        assert "install_sigterm_notify(finish)" in src, name
