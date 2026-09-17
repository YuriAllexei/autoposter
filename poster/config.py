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

# User rule (2026-09-17): random delays live between 3-7s; NOTHING may wait
# longer than this cap, even if .env asks for more.
MAX_ALLOWED_DELAY = 7.0


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
    fb_lang: str = "es"

    groups_file: Path = field(default=Path("data/groups.json"))
    post_text_file: Path = field(default=Path("data/post.txt"))
    photos_dir: Path = field(default=Path("data/car_photos"))
    photo_extensions: tuple[str, ...] = (".jpg", ".jpeg", ".png", ".webp")
    max_photos_per_post: int = 10

    delay_min: float = 45.0
    delay_max: float = 180.0
    type_delay_min_ms: int = 30
    type_delay_max_ms: int = 90

    log_dir: Path = field(default=Path(".local-capture/logs"))
    screenshot_dir: Path = field(default=Path(".local-capture/shots"))
    dump_on_error: bool = True

    @property
    def repo_root(self) -> Path:
        return REPO_ROOT


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
        fb_lang=(get("AP_FB_LANG") or "es").strip().lower(),
        groups_file=_as_path(get("AP_GROUPS_FILE"), "data/groups.json"),
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
        type_delay_min_ms=_as_int(get("AP_TYPE_DELAY_MIN_MS"), 30),
        type_delay_max_ms=_as_int(get("AP_TYPE_DELAY_MAX_MS"), 90),
        log_dir=_as_path(get("AP_LOG_DIR"), ".local-capture/logs"),
        screenshot_dir=_as_path(get("AP_SCREENSHOT_DIR"), ".local-capture/shots"),
        dump_on_error=_as_bool(get("AP_DUMP_ON_ERROR"), True),
    )
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
    return cfg
