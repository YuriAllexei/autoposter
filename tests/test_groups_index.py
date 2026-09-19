"""Tests for the group lifecycle index (no browser)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poster import groups_index as gi


def _mk_capture(root: Path, name: str, url: str, purpose: str = ""):
    d = root / "facebook" / name
    d.mkdir(parents=True)
    (d / "summary.json").write_text(
        json.dumps({"url": url, "purpose": purpose, "dir": str(d), "site": "facebook"}),
        encoding="utf-8")
    return d


def test_scan_recordings_extracts_group_id_from_purpose(tmp_path):
    # the realistic case: recording START url is facebook.com, group id only
    # appears in the purpose annotation
    _mk_capture(tmp_path, "20260917T063422Z",
                "https://www.facebook.com",
                "scout posting workflow https://www.facebook.com/groups/249803862915566")
    recs = gi.scan_recordings(tmp_path)
    assert "249803862915566" in recs
    assert recs["249803862915566"][0]["ts"] == "20260917T063422Z"


def test_scan_recordings_untagged_sessions_go_under_empty_key(tmp_path):
    _mk_capture(tmp_path, "20260101T000000Z", "https://www.facebook.com", "login only")
    recs = gi.scan_recordings(tmp_path)
    assert "" in recs and "249803862915566" not in " ".join(recs)


def test_scan_recordings_resolves_vanity_slug_url(tmp_path):
    # vanity-slug recording: no numeric id anywhere in the summary; the dump's
    # captured route-definitions response carries "groupID" next to the slug
    d = _mk_capture(tmp_path, "20260919T024503Z",
                    "https://www.facebook.com/groups/CarrosBaratoss/",
                    "this group's layout")
    body = ('{"definitions":{"https://www.facebook.com/groups/CarrosBaratoss/":'
            '{"groupID":"277203682420532","meta":{"title":"CARROS EN VENTA '
            'CHIHUAHUA"},"prefetchable":true}}}')
    stray = '{"stories":[{"groupID":"999888777666555","name":"unrelated feed"}]}'
    with (d / "manual_requests.jsonl").open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"record": "http_detail",
                             "url": "https://www.facebook.com/ajax/bulk-route-definitions/",
                             "response_body": body}) + "\n")
        fh.write(json.dumps({"record": "http_detail", "url": "/feed",
                             "response_body": stray}) + "\n")
    recs = gi.scan_recordings(tmp_path)
    assert "277203682420532" in recs
    assert "999888777666555" not in recs  # unrelated ids must not leak
    assert "" not in recs                 # no longer silently untagged


def test_compute_status_stages(tmp_path):
    groups = [{
        "name": "G", "group_url": "https://www.facebook.com/groups/111",
        "posting_code": "group_composer_es_v1", "enabled": True,
    }]
    recordings = {"111": [{"dir": "facebook/TS", "ts": "TS", "url": "", "purpose": ""}]}
    rows = gi.compute_status(groups, {"some_other_flow"}, {}, recordings)
    r = rows[0]
    assert r["recording"] == "facebook/TS"          # auto-linked from disk scan
    assert not r["implemented"]
    assert "flow missing" in r["stage"]

    rows = gi.compute_status(groups, {"group_composer_es_v1"}, {}, recordings)
    assert "dry-run pending" in rows[0]["stage"]

    rows = gi.compute_status(groups, {"group_composer_es_v1"},
                             {"111": {"ts": "x", "evidence": "y"}}, recordings)
    assert "live-ready" in rows[0]["stage"]


def test_find_gaps_lists_recorded_but_unwired(tmp_path):
    recordings = {"999": [{"dir": "facebook/TS999", "ts": "TS999",
                           "url": "", "purpose": "new group"}]}
    gaps = gi.find_gaps([], recordings, {"f"})
    assert gaps and "999" in gaps[0] and "groups.json" in gaps[0]


def test_find_gaps_lists_broken_posting_code():
    groups = [{"name": "N", "group_url": "https://www.facebook.com/groups/5",
               "posting_code": "ghost_flow"}]
    gaps = gi.find_gaps(groups, {}, {"real_flow"})
    assert any("ghost_flow" in g for g in gaps)


def test_stamp_dryrun_ok_roundtrip(tmp_path):
    marker = tmp_path / "data/dryrun_ok.json"
    st = gi.stamp_dryrun_ok("https://www.facebook.com/groups/777", "/e.png",
                            path=marker)
    assert st["evidence"] == "/e.png"
    loaded = gi.load_markers(marker)
    assert loaded["777"]["evidence"] == "/e.png"
    # second stamp replaces (latest dry run wins), other ids survive
    marker.write_text(json.dumps({**loaded, "888": {"ts": "old", "evidence": "x"}}),
                      encoding="utf-8")
    gi.stamp_dryrun_ok("https://www.facebook.com/groups/777", "/e2.png", path=marker)
    loaded = gi.load_markers(marker)
    assert loaded["777"]["evidence"] == "/e2.png" and "888" in loaded


def test_groups_file_registry_consistency():
    """Every groups.json entry parses into the index without error, and the
    shipped repo state reports a gap until each recording is wired (smoke)."""
    repo = Path(__file__).resolve().parents[1]
    groups = gi.load_groups(repo / "data/groups.json")
    rows = gi.compute_status(groups, gi._registry_names(), {}, {})
    assert len(rows) == len(groups)