"""Make every test hermetic against ambient AP_* environment variables.

poster.config.load_config intentionally prefers real environment over any
.env file. This host's shell environment carries a full copy of the repo
.env (Hermes terminal env injection), which silently overrode every
env_file= argument in the config tests — test_delay_hard_capped_at_7s only
"passed" while the ambient snapshot happened to equal the 7s cap. Stripping
AP_* per test keeps assertions about .env parsing meaningful.
"""
import os

import pytest


@pytest.fixture(autouse=True)
def _hermetic_ap_env(monkeypatch):
    for key in [k for k in os.environ if k.startswith("AP_")]:
        monkeypatch.delenv(key, raising=False)
