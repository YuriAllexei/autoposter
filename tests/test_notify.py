"""Unit tests for the Discord notifier (payload build; network mocked)."""
import json
import urllib.error

from poster import notify
from poster.notify import EMBED_DESC_LIMIT, build_payload


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
