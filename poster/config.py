"""Config loader — .env (AP_* keys) + defaults, fail-safe.

Precedence: real environment > .env file > defaults. An absent/invalid
AP_DRY_RUN falls back to True: the bot never enables posting by accident;
going live requires explicitly writing AP_DRY_RUN=false.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_TRUTHY = {"1", "true", "yes", "on"}

# User rule (2026-09-22): random delays live between 2-4s (was 3-7s); NOTHING
# may wait longer than this cap, even if .env asks for more.
MAX_ALLOWED_DELAY = 7.0

# Exception to the rule above (also user-specified 2026-09-22): the
# group-to-group switch sleep is 10-15s by design. This is its own safety
# ceiling so a typo in .env can never park the bot for minutes.
MAX_ALLOWED_GROUP_SWITCH = 20.0


def _as_bool(v: str | None, default: bool) -> bool:
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in _TRUTHY


def _as_float(v: str | None, default: float) -> float:
    try:
        return float(v)  # type: ignore[arg-type]  # None raises TypeError -> default
    except (TypeError, ValueError):
        return default


def _as_int(v: str | None, default: int) -> int:
    try:
        return int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_path(v: str | None, default: str) -> Path:
    p = Path(v if v and v.strip() else default)
    return p if p.is_absolute() else REPO_ROOT / p


def parse_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser (comments, blanks, optional `export `, quotes)."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            out[key] = val
    return out


@dataclass
class Config:
    dry_run: bool = True
    headless: bool = False
    max_posts_per_run: int = 1

    profile_dir: Path = field(default=Path(".local-capture/profiles/facebook"))
    viewport_width: int = 1280
    viewport_height: int = 900

    fb_main_user: str = ""
    fb_posting_user: str = ""
    fb_posting_profile_name: str = "Carmazon"
    # Account-menu ROW TEXT of the personal profile when it coexists with the
    # page in the switcher (recording 20260922T210535Z: rows are "Carmazon"
    # and "Carmazon Alex" — NOT the legal name). Used by AP_POST_AS=profile.
    fb_main_profile_name: str = ""
    # Which identity posts: "page" = professional profile (Carmazon) or
    # "profile" = the PERSONAL login itself. Acting identity is read from
    # the `av` param on /api/graphql requests (see poster/fb.py:identity_ok).
    post_as: str = "page"
    fb_lang: str = "es"

    post_text_file: Path = field(default=Path("data/post.txt"))
    photos_dir: Path = field(default=Path("data/car_photos"))
    photo_extensions: tuple[str, ...] = (".jpg", ".jpeg", ".png", ".webp")
    max_photos_per_post: int = 10

    delay_min: float = 45.0
    delay_max: float = 180.0
    # Explicit user rule (2026-09-22): the wait BETWEEN finishing one group and
    # opening the next is uniform 10-15s (separate category from the general
    # 2-4s action sleeps; still safety-capped, see MAX_ALLOWED_GROUP_SWITCH).
    group_switch_min: float = 10.0
    group_switch_max: float = 15.0
    type_delay_min_ms: int = 30
    type_delay_max_ms: int = 90

    log_dir: Path = field(default=Path(".local-capture/logs"))
    screenshot_dir: Path = field(default=Path(".local-capture/shots"))
    dump_on_error: bool = True

    # ---- monitoring (see poster/results.py, poster/notify.py) ----
    ledger_file: Path = field(
        default=Path(".local-capture/results/ledger.jsonl"))
    discord_webhook_url: str = ""

    @property
    def repo_root(self) -> Path:
        return REPO_ROOT

    @property
    def identity_label(self) -> str:
        """Name of the identity we post as, for logs + the Discord embed."""
        return (self.fb_main_profile_name if self.post_as == "profile"
                else self.fb_posting_profile_name)

    @property
    def identity_id(self) -> str:
        """The `av` id of the posting identity — the SAME value both the
        joins-fetch and the composer run must act as (one truth, one av)."""
        return (self.fb_main_user if self.post_as == "profile"
                else self.fb_posting_user)


def load_config(env_file: Path | None = None) -> Config:
    """Build Config from defaults + .env file + process environment."""
    file_vars = parse_env_file(env_file or (_as_path(os.environ.get("AP_ENV_FILE"), ".env")))

    def get(key: str) -> str | None:
        return os.environ.get(key, file_vars.get(key))

    cfg = Config(
        dry_run=_as_bool(get("AP_DRY_RUN"), True),
        headless=_as_bool(get("AP_HEADLESS"), False),
        max_posts_per_run=_as_int(get("AP_MAX_POSTS_PER_RUN"), 1),
        profile_dir=_as_path(get("AP_PROFILE_DIR"), ".local-capture/profiles/facebook"),
        viewport_width=_as_int(get("AP_VIEWPORT_WIDTH"), 1280),
        viewport_height=_as_int(get("AP_VIEWPORT_HEIGHT"), 900),
        fb_main_user=(get("AP_FB_MAIN_USER") or "").strip(),
        fb_posting_user=(get("AP_FB_POSTING_USER") or "").strip(),
        fb_posting_profile_name=(get("AP_FB_POSTING_PROFILE_NAME") or "Carmazon").strip(),
        fb_main_profile_name=(get("AP_FB_MAIN_PROFILE_NAME") or "").strip(),
        post_as=(get("AP_POST_AS") or "page").strip().lower(),
        fb_lang=(get("AP_FB_LANG") or "es").strip().lower(),
        post_text_file=_as_path(get("AP_POST_TEXT_FILE"), "data/post.txt"),
        photos_dir=_as_path(get("AP_PHOTOS_DIR"), "data/car_photos"),
        photo_extensions=tuple(
            e.strip().lower() if e.strip().startswith(".") else "." + e.strip().lower()
            for e in (get("AP_PHOTO_EXTENSIONS") or ".jpg,.jpeg,.png,.webp").split(",")
            if e.strip()
        ),
        max_photos_per_post=_as_int(get("AP_MAX_PHOTOS_PER_POST"), 10),
        delay_min=_as_float(get("AP_DELAY_MIN_SECONDS"), 45.0),
        delay_max=_as_float(get("AP_DELAY_MAX_SECONDS"), 180.0),
        group_switch_min=_as_float(get("AP_GROUP_SWITCH_MIN_SECONDS"), 10.0),
        group_switch_max=_as_float(get("AP_GROUP_SWITCH_MAX_SECONDS"), 15.0),
        type_delay_min_ms=_as_int(get("AP_TYPE_DELAY_MIN_MS"), 30),
        type_delay_max_ms=_as_int(get("AP_TYPE_DELAY_MAX_MS"), 90),
        log_dir=_as_path(get("AP_LOG_DIR"), ".local-capture/logs"),
        screenshot_dir=_as_path(get("AP_SCREENSHOT_DIR"), ".local-capture/shots"),
        dump_on_error=_as_bool(get("AP_DUMP_ON_ERROR"), True),
        ledger_file=_as_path(get("AP_LEDGER_FILE"),
                             ".local-capture/results/ledger.jsonl"),
        discord_webhook_url=(get("AP_DISCORD_WEBHOOK_URL") or "").strip(),
    )
    if cfg.post_as not in ("page", "profile"):
        raise ValueError(
            f"invalid AP_POST_AS={cfg.post_as!r} (use 'page' or 'profile')")
    if cfg.delay_min < 0 or cfg.delay_max < cfg.delay_min:
        raise ValueError(
            f"invalid delay config: AP_DELAY_MIN_SECONDS={cfg.delay_min} "
            f"AP_DELAY_MAX_SECONDS={cfg.delay_max} (need 0 <= min <= max)"
        )
    # HARD cap (user rule): no wait in this project exceeds MAX_ALLOWED_DELAY,
    # regardless of what .env says. Always leaves a random spread.
    cfg.delay_max = min(cfg.delay_max, MAX_ALLOWED_DELAY)
    if cfg.delay_min >= cfg.delay_max:
        cfg.delay_min = max(0.0, cfg.delay_max - 4.0)
    if cfg.group_switch_min < 0 or cfg.group_switch_max < cfg.group_switch_min:
        raise ValueError(
            f"invalid group-switch config: AP_GROUP_SWITCH_MIN_SECONDS="
            f"{cfg.group_switch_min} AP_GROUP_SWITCH_MAX_SECONDS="
            f"{cfg.group_switch_max} (need 0 <= min <= max)")
    cfg.group_switch_max = min(cfg.group_switch_max, MAX_ALLOWED_GROUP_SWITCH)
    cfg.group_switch_min = min(cfg.group_switch_min, cfg.group_switch_max)
    return cfg
