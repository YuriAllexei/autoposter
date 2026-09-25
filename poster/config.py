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
#: crosspost batch/listing gap is its own category (user spec 2026-09-23:
#: ~2 min between batches of one listing AND between listings); capped.
MAX_ALLOWED_CROSSPOST_GAP = 300.0

#: share-to-share gap is its own category (user spec 2026-09-24: 2-3s between
#: one share and the next — group to group AND listing to listing); own
#: ceiling so a .env typo can never park the share pipeline for minutes. The
#: general MAX_ALLOWED_DELAY (7s) deliberately does NOT apply here, exactly
#: like the crosspost gap above.
MAX_ALLOWED_SHARE_GAP = 20.0


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

    # ---- marketplace crosspost pipeline (poster/crosspost.py) ----
    # Explicit user rule (2026-09-23): ALL batches run in one go, separated
    # by ~2 minutes. Capped at MAX_ALLOWED_CROSSPOST_GAP like every other wait.
    crosspost_gap_min: float = 110.0
    crosspost_gap_max: float = 130.0
    #: 0 = every active listing per run (user decision: all in one run)
    crosspost_max_listings: int = 0
    #: USER SPEC (2026-09-23): sleeps BETWEEN crosspost actions are their own
    #: category — uniform 1-3s (not the 2-4s text-post range). Checkbox rows
    #: inside the dialog get a faster 0.2-1s micro-pause (constant lives in
    #: poster/flows.py; they are rapid human ticks, not actions).
    crosspost_action_min: float = 1.0
    crosspost_action_max: float = 3.0

    # ---- individual SHARE pipeline (poster/share.py) ----
    # Explicit user rule (2026-09-24): the wait BETWEEN one share and the
    # next — group to group AND listing to listing — is its own category,
    # uniform 2-3s (the 10-15s group-change category is NOT used here).
    # Capped at MAX_ALLOWED_SHARE_GAP like every other wait.
    share_gap_min: float = 2.0
    share_gap_max: float = 3.0

    log_dir: Path = field(default=Path(".local-capture/logs"))
    screenshot_dir: Path = field(default=Path(".local-capture/shots"))

    # ---- monitoring (see poster/results.py, poster/notify.py) ----
    ledger_file: Path = field(
        default=Path(".local-capture/results/ledger.jsonl"))
    discord_webhook_url: str = ""

    def __post_init__(self) -> None:
        """The user-rule guards live HERE, not in the loader: no Config —
        from .env, tests or direct construction — may carry an unknown
        identity mode or a wait past the caps."""
        if self.post_as not in ("page", "profile"):
            raise ValueError(
                f"invalid AP_POST_AS={self.post_as!r} (use 'page' or 'profile')")
        if self.delay_min < 0 or self.delay_max < self.delay_min:
            raise ValueError(
                f"invalid delay config: AP_DELAY_MIN_SECONDS={self.delay_min} "
                f"AP_DELAY_MAX_SECONDS={self.delay_max} (need 0 <= min <= max)")
        # HARD cap (user rule): no general wait exceeds MAX_ALLOWED_DELAY,
        # whatever .env says. Always leaves a random spread.
        self.delay_max = min(self.delay_max, MAX_ALLOWED_DELAY)
        if self.delay_min >= self.delay_max:
            self.delay_min = max(0.0, self.delay_max - 4.0)
        if (self.group_switch_min < 0
                or self.group_switch_max < self.group_switch_min):
            raise ValueError(
                f"invalid group-switch config: AP_GROUP_SWITCH_MIN_SECONDS="
                f"{self.group_switch_min} AP_GROUP_SWITCH_MAX_SECONDS="
                f"{self.group_switch_max} (need 0 <= min <= max)")
        self.group_switch_max = min(self.group_switch_max, MAX_ALLOWED_GROUP_SWITCH)
        self.group_switch_min = min(self.group_switch_min, self.group_switch_max)
        if (self.crosspost_action_min < 0
                or self.crosspost_action_max < self.crosspost_action_min):
            raise ValueError(
                "invalid crosspost-action sleep config: "
                "AP_CROSSPOST_ACTION_MIN_SECONDS="
                f"{self.crosspost_action_min} AP_CROSSPOST_ACTION_MAX_SECONDS="
                f"{self.crosspost_action_max} (need 0 <= min <= max)")
        self.crosspost_action_max = min(self.crosspost_action_max,
                                        MAX_ALLOWED_DELAY)
        self.crosspost_action_min = min(self.crosspost_action_min,
                                        self.crosspost_action_max)
        if (self.crosspost_gap_min < 0
                or self.crosspost_gap_max < self.crosspost_gap_min):
            raise ValueError(
                f"invalid crosspost-gap config: AP_CROSSPOST_GAP_MIN_SECONDS="
                f"{self.crosspost_gap_min} AP_CROSSPOST_GAP_MAX_SECONDS="
                f"{self.crosspost_gap_max} (need 0 <= min <= max)")
        self.crosspost_gap_max = min(self.crosspost_gap_max,
                                     MAX_ALLOWED_CROSSPOST_GAP)
        self.crosspost_gap_min = min(self.crosspost_gap_min,
                                     self.crosspost_gap_max)
        if (self.share_gap_min < 0
                or self.share_gap_max < self.share_gap_min):
            raise ValueError(
                f"invalid share-gap config: AP_SHARE_GAP_MIN_SECONDS="
                f"{self.share_gap_min} AP_SHARE_GAP_MAX_SECONDS="
                f"{self.share_gap_max} (need 0 <= min <= max)")
        self.share_gap_max = min(self.share_gap_max, MAX_ALLOWED_SHARE_GAP)
        self.share_gap_min = min(self.share_gap_min, self.share_gap_max)

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


def load_config(env_file: Path | str | None = None) -> Config:
    """Build Config from defaults + .env file + process environment.
    `env_file` accepts str too (argparse hands us raw --env-file strings)."""
    if env_file is not None:
        env_file = Path(env_file)
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
        crosspost_gap_min=_as_float(get("AP_CROSSPOST_GAP_MIN_SECONDS"), 110.0),
        crosspost_gap_max=_as_float(get("AP_CROSSPOST_GAP_MAX_SECONDS"), 130.0),
        crosspost_max_listings=_as_int(get("AP_CROSSPOST_MAX_LISTINGS"), 0),
        crosspost_action_min=_as_float(
            get("AP_CROSSPOST_ACTION_MIN_SECONDS"), 1.0),
        crosspost_action_max=_as_float(
            get("AP_CROSSPOST_ACTION_MAX_SECONDS"), 3.0),
        share_gap_min=_as_float(get("AP_SHARE_GAP_MIN_SECONDS"), 2.0),
        share_gap_max=_as_float(get("AP_SHARE_GAP_MAX_SECONDS"), 3.0),
        type_delay_min_ms=_as_int(get("AP_TYPE_DELAY_MIN_MS"), 30),
        type_delay_max_ms=_as_int(get("AP_TYPE_DELAY_MAX_MS"), 90),
        log_dir=_as_path(get("AP_LOG_DIR"), ".local-capture/logs"),
        screenshot_dir=_as_path(get("AP_SCREENSHOT_DIR"), ".local-capture/shots"),
        ledger_file=_as_path(get("AP_LEDGER_FILE"),
                             ".local-capture/results/ledger.jsonl"),
        discord_webhook_url=(get("AP_DISCORD_WEBHOOK_URL") or "").strip(),
    )
    return cfg
