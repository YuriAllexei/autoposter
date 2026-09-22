"""Pins for the dynamic joins-fetch (real proof = live --list-groups run)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poster.config import load_config
from poster.groups_fetch import DOC_ID_JOINS, FRIENDLY_NAME, PAG_DOC_ID, PAG_FRIENDLY

REPO = Path(__file__).resolve().parents[1]


def test_doc_id_pinned_to_recording():
    # recording 20260922T214219Z_groups_fetcher — if this changes, re-verify
    # against a fresh recording, don't just edit the number
    assert DOC_ID_JOINS == "24648931168042404"
    assert FRIENDLY_NAME == "GroupsCometJoinsRootQuery"


def test_pagination_doc_pinned_to_live_sniff():
    # the ROOT doc ignores `after` (returns page 1 forever); the joins tab's
    # own 'load more' is this SECOND doc with {count, cursor} [sniffed live
    # 2026-09-22]. Without it we silently post to only the first 20 groups.
    assert PAG_DOC_ID == "9974006939348139"
    assert PAG_FRIENDLY == "GroupsCometAllJoinedGroupsSectionPaginationQuery"


def test_identity_id_tracks_post_as(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("AP_FB_MAIN_USER=111\nAP_FB_POSTING_USER=222\nAP_POST_AS=profile\n")
    cfg = load_config(env_file=env)
    assert cfg.identity_id == "111"
    env.write_text("AP_FB_MAIN_USER=111\nAP_FB_POSTING_USER=222\nAP_POST_AS=page\n")
    cfg = load_config(env_file=env)
    assert cfg.identity_id == "222"
