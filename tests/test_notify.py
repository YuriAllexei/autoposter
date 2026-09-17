"""Unit tests for the Discord notifier (payload build; network mocked)."""
import json
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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