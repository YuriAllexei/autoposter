"""identity_ok — pure predicate for which FB identity is posting (no browser).

Ground truth (recording 20260922T210535Z, profile_switcher):
  av=61592323007979  while acting as the CARMASON PAGE   (i_user=615...79)
  av=61592579496197  while acting as PERSONAL profile    (no i_user; c_user=...)
The switcher menu lists rows "Carmazon" and "Carmazon Alex" in EITHER state.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from poster.config import load_config
from poster.fb import identity_ok

MAIN = "61592579496197"
CARM = "61592323007979"


def test_page_mode_uses_av_when_seen():
    assert identity_ok({"c_user": MAIN}, post_as="page",
                       posting_user_id=CARM, main_user_id=MAIN, av=CARM)
    assert not identity_ok({"c_user": MAIN}, post_as="page",
                           posting_user_id=CARM, main_user_id=MAIN, av=MAIN)


def test_profile_mode_uses_av_when_seen():
    assert identity_ok({"c_user": MAIN}, post_as="profile",
                       posting_user_id=CARM, main_user_id=MAIN, av=MAIN)
    # still on the page -> NOT the personal identity
    assert not identity_ok({"c_user": MAIN}, post_as="profile",
                           posting_user_id=CARM, main_user_id=MAIN, av=CARM)


def test_av_authoritative_over_stale_i_user():
    # stale i_user cookie must not override a good av
    assert identity_ok({"c_user": MAIN, "i_user": CARM}, post_as="profile",
                       posting_user_id=CARM, main_user_id=MAIN, av=MAIN)


def test_cookie_fallback_page_mode():
    assert identity_ok({"c_user": MAIN, "i_user": CARM}, post_as="page",
                       posting_user_id=CARM, main_user_id=MAIN)
    assert not identity_ok({"c_user": MAIN}, post_as="page",
                           posting_user_id=CARM, main_user_id=MAIN)


def test_cookie_fallback_profile_mode():
    # no av seen yet: personal = c_user==main AND no i_user
    assert identity_ok({"c_user": MAIN}, post_as="profile",
                       posting_user_id=CARM, main_user_id=MAIN)
    assert not identity_ok({"c_user": MAIN, "i_user": CARM}, post_as="profile",
                           posting_user_id=CARM, main_user_id=MAIN)


def test_missing_ids_never_pass():
    assert not identity_ok({"c_user": MAIN}, post_as="profile",
                           posting_user_id=CARM, main_user_id="", av=None)
    assert not identity_ok({"c_user": MAIN, "i_user": CARM}, post_as="page",
                           posting_user_id="", main_user_id=MAIN, av=None)
    # av present but target id empty -> no match, never assume
    assert not identity_ok({"c_user": MAIN}, post_as="profile",
                           posting_user_id=CARM, main_user_id="", av=MAIN)


def test_default_mode_is_page():
    cfg = load_config(env_file=Path("/nonexistent/.env"))
    assert cfg.post_as == "page"
    assert cfg.fb_main_profile_name == ""


def test_profile_mode_main_profile_name_parses(tmp_path):
    env = tmp_path / ".env"
    env.write_text("AP_POST_AS=profile\nAP_FB_MAIN_PROFILE_NAME=Carmazon Alex\n")
    cfg = load_config(env_file=env)
    assert cfg.post_as == "profile"
    assert cfg.fb_main_profile_name == "Carmazon Alex"


def test_invalid_post_as_raises(tmp_path):
    env = tmp_path / ".env"
    env.write_text("AP_POST_AS=bogus\n")
    with pytest.raises(ValueError):
        load_config(env_file=env)
