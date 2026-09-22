"""Pins for the dynamic joins-fetch (real proof = live --list-groups run)."""
from pathlib import Path

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

