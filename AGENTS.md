# autoposter — Agent Guide

Facebook group auto-posting bot (Playwright + persistent Firefox profile).
Each run: becomes the configured identity ("Carmazon Alex" profile or
"Carmazon" page) → fetches its joined groups LIVE → rotates through them
(never-attempted first, then oldest ledger attempt, capped) → posts the
car-inventory summary. Flows come from human recordings via the
`scraping_recorder` submodule and are keyed by composer LAYOUT, not by group
(all Spanish groups share one identical composer).

## Environment / commands

- WSL + WSLg (headed browsers OK). **Poetry manages everything: run all Python
  via `poetry run ...`, never bare `python3`/`pip`.**
- **New machine (teammate): clone → `git submodule update --init` → `./setup.sh`
  → edit `.env` → `ap-record` and log into FB by hand once** (creates the
  persistent profile incl. 2FA; cookies, never passwords, and they are NOT
  shared between machines — each dev logs in on their own box).

```bash
# THE BOT. DRY RUN = stages composer (text verified 1:1 + photos attached)
# then closes it WITHOUT clicking Publicar + saves evidence under
# .local-capture/shots/:
poetry run python -m poster.main              # respects AP_DRY_RUN in .env
poetry run python -m poster.main --dry-run    # force dry run
poetry run python -m poster.main --group 249803862915566   # only this group
poetry run python -m poster.main --live       # only works if .env has AP_DRY_RUN=false
poetry run python -m poster.main --list-groups  # joined groups + rotation state (alias: ap-groups)

# Exit codes: 0 posted ≥1 · 1 nothing posted / missing post.txt · 2 not
# logged in · 3 identity switch failed · 4 joined-groups fetch failed.

# RECORD a flow (ground truth; NEVER commit dumps — they hold cookies).
# --url optional (no url = bare browser). Dumps land under
# <site>/<UTCstamp>[_<label>]/; q+Enter prompts for purpose/label.
poetry run python scraping_recorder/record_session.py \
  [--url https://www.facebook.com/groups/<GROUP_ID>] \
  --profile .local-capture/profiles/facebook \
  --out .local-capture/manual_session --trace
poetry run python scraping_recorder/session_timeline.py <dump_dir> --full
poetry run python scripts/probe_group.py [--group-url URL]  # adopt identity + describe a group's posting UI (never clicks/post)
poetry run python scripts/dump_header.py  # header buttons for selector work

# DEV LOOP — must pass before any commit:
poetry run pytest tests scraping_recorder/tests -q
poetry run ruff check poster tests scripts

# MONITORING: after EVERY finished run (aborts/crashes included) the bot
# posts ONE Discord embed: ✅/🧪/❌/⏭️ per group + all-time published count
# per group. Channel webhook only (AP_DISCORD_WEBHOOK_URL in .env).
# Ledger = .local-capture/results/ledger.jsonl (gitignored, one line per
# attempt: run_id/ts/group_id/name/status/error/dry_run;
# status = published|staged|failed|skipped):
poetry run python -m poster.notify --test   # fake summary, verify webhook
poetry run python -m poster.notify --stats  # all-time published per group
# GOTCHA: Discord/Cloudflare 403 (error 1010) on urllib's default
# User-Agent — poster/notify.py sends a custom one; never "simplify" away.
```

Shell helpers (`shell/autoposter.sh`, sourced by setup.sh; bash+zsh):
`ap-record [URL]`, `ap-timeline [dir] [--full]`, `ap-groups`.
Recorder console: `s`+Enter screenshot+note, `q`+Enter quit & flush.
Submodule gotchas: see `scraping_recorder/AGENTS.md`.

## Posting rules (user spec — non-negotiable)

1. ONE post per group per run; content = `data/post.txt` copied 1:1 (no reformat).
2. Photos `data/car_photos/NN_car/` — folders sorted = car order in post.txt,
   files sorted inside each car; upload one batch per car folder.
3. OS file dialog: never let it open — `page.on("filechooser")` →
   `fc.set_files(...)`; fallback `set_input_files` on `input[type=file]`.
4. TARGETS = the identity's JOINED groups fetched live each run
   (`poster/groups_fetch.py`), rotated never-attempted-first then
   oldest-ledger-attempt, capped `AP_MAX_POSTS_PER_RUN`. The SOLE per-group
   gate: the 'Escribe algo...' trigger must render within 5s of the group
   page, else the group records `skipped` (not an error). A goto that broke
   counts as `failed`, never as `skipped`. If the joins fetch itself fails,
   the run ABORTS (exit 4 + Discord) — a broken fetch must never mean
   "post to nothing". A layout that differs from the proven one (e.g. the
   marketplace 'Vender algo' composer) needs a recording + new flow fn —
   never guess; until then it is skipped with a marketplace reason.
5. Anti-detection sleeps: `human_sleep()` = uniform(`AP_DELAY_MIN`,`AP_DELAY_MAX`)
   before sensitive steps; operating range 2-4s and `Config.__post_init__`
   hard-caps general waits at 7s (no Config, however built, exceeds it).
   GROUP CHANGE is its own category: 10-15s (`AP_GROUP_SWITCH_*`, cap 20s)
   after finishing one group, never after the last. NO sleep between photo
   batches (settling = `_wait_upload_settled` condition-wait). Post text is
   PASTED (`execCommand('insertText')`, Enter per line) behind the 1:1
   read-back gate; char-by-char typing survives only as one-shot fallback.
6. DEV SAFETY: never post or comment unless the selector is proven against a
   recording/dry-run. `AP_DRY_RUN=true` is default and fail-safe.
7. MONITORING: exactly ONE Discord summary per run, sent only after it ends
   (success/failure/abort/crash all notify). Dry-run stages never count as
   published; `skipped` never counts. Webhook failure never changes rc.

## Protocol facts (ground truth from recordings; verify against them before
## "fixing" selectors)

- UI is Spanish here: trigger `Escribe algo...`, publish `Publicar`. Match
  text/aria, NEVER obfuscated classes.
- Identity MODES (`AP_POST_AS`; recording 20260922T210535Z): the acting
  identity is the `av` param on /api/graphql requests — av=61592323007979
  (Carmazon page) vs av=61592579496197 (personal, == c_user). The `i_user`
  cookie exists ONLY for the page and CANNOT detect the personal switch.
  The account-menu lists BOTH rows always: "Carmazon" and "Carmazon Alex"
  (= the personal profile's row text; NOT the legal name) →
  `AP_FB_MAIN_PROFILE_NAME`. 'Carmazon' ⊂ 'Carmazon Alex', so row matching
  is EXACT-text-first (`CLICK_PROFILE_ITEM_JS`). Composer flow identical in
  both modes.
- Profile switch works ONLY via: click `[aria-label="Tu perfil"]`, stamp the
  target menu row `data-ap-switch`, then Playwright locator `.click()` —
  JS-dispatched `.click()` and raw `mouse.click(x,y)` both fail
  (untrusted events / no auto-scroll).
- Joined-groups fetch (recording 20260922T214219Z): page 1 =
  `GroupsCometJoinsRootQuery` doc, which IGNORES cursors (20 max); further
  pages = `GroupsCometAllJoinedGroupsSectionPaginationQuery` doc with
  `{count, cursor}` vars — both doc_ids pinned in `poster/groups_fetch.py`.
  Anti-CSRF tokens: `fb_dtsg`/`lsd` are NOT on `window` — resolve via
  `require('DTSGInitialData'/'LSD').token`, fallback regex over the boot
  HTML; `jazoest` = "2" + Σ charCodes(dtsg).
- MARKETPLACE TAB gotcha (found 2026-09-22, evidence
  shots/no_composer_20260922T235159Z.png): buy/sell groups may show
  'Vender algo' (sell-form composer) instead of 'Escribe algo' — same
  group, different layout state; posting there is NOT proven, so the gate
  skips it (with the marketplace reason).
- Login lands on the MAIN account; login is COOKIES ONLY (manual first login
  in the headed profile; the bot never types credentials — no
  AP_FB_EMAIL/AP_FB_PASSWORD exists in config).
- Photo attach (recording 20260917T063422Z): composer dialog holds a hidden
  `input[type=file]`; picks POST to
  `upload.facebook.com/ajax/react_composer/attachments/photo/upload`.
- Group posts go through admin approval → live runs save
  `/groups/<id>/my_pending_content/` evidence after publishing.

## Adding groups

No file to edit: join with the posting identity in the real browser and the
next run picks it up from the live joins fetch (order = FB viewer_added).
`ap-groups` shows every joined group + rotation state. Recordings are ONLY
for an unseen composer LAYOUT, never per group.

## Conventions

- All knobs in `.env` (`AP_` prefix, see `.env.example`; real env always wins,
  `AP_ENV_FILE` relocates the file). `.env`, `.local-capture/**`, and
  `data/car_photos/**` images are gitignored.
- Next up the roadmap: scheduler + delivery verification (parse
  my_pending_content so "published" can mean admin-approved).
