# autoposter — Agent Guide

Facebook group auto-posting bot (Playwright + persistent Firefox profile).
Posts our car-inventory summary as the professional profile "Carmazon" into
groups listed in `data/groups.json`. Per-group layouts come from human
recordings via the `scraping_recorder` submodule; flows can differ per group.

## Environment / commands

- WSL + WSLg (headed browsers OK). **Poetry manages everything: run all Python
  via `poetry run ...`, never bare `python3`/`pip`.**
- **New machine (teammate): clone → `git submodule update --init` → `./setup.sh`
  → edit `.env` → `ap-record` and log into FB by hand once** (creates the
  persistent profile incl. 2FA; cookies, never passwords, and they are NOT
  shared between machines — each dev logs in on their own box).

```bash
# record a group flow (ground truth; NEVER commit dumps — they hold cookies).
# --url is optional (no url = bare browser). --out keeps dumps in the parent
# repo; layout under it: <site>/<UTCstamp>[_<label>]/. q+Enter prompts for
# purpose/label (skip with --no-prompt / use --purpose/--label).
poetry run python scraping_recorder/record_session.py \
  [--url https://www.facebook.com/groups/<GROUP_ID>] \
  --profile .local-capture/profiles/facebook \
  --out .local-capture/manual_session --trace
poetry run python scraping_recorder/session_timeline.py <dump_dir> --full
poetry run python scripts/probe_group.py [--group-url URL]  # read-only composer probe
poetry run python scripts/dryrun_post.py   # full flow up to (never incl.) Publicar
poetry run python scripts/dump_header.py   # header buttons for selector work

# THE BOT (poster package). DRY RUN = stages composer (text verified 1:1 +
# photos attached) then closes it WITHOUT clicking Publicar + saves evidence
# screenshots/html/json under .local-capture/shots/:
poetry run python -m poster.main                # respects AP_DRY_RUN in .env
poetry run python -m poster.main --dry-run      # force dry run
poetry run python -m poster.main --group 249803862915566
# --live only works when .env already has AP_DRY_RUN=false (fail-safe by design)
poetry run python -m poster.main --live

# GROUP INDEX: per-group pipeline state (recording found on disk? flow fn in
# REGISTRY? dry-run stamp in data/dryrun_ok.json?) + GAPS: recordings never
# wired into groups.json, entries whose posting_code has no function.
# The stamp file is per-machine (gitignored): a fresh clone starts unverified.
poetry run python -m poster.main --status      # alias: ap-status

poetry run pytest tests scraping_recorder/tests -q

# MONITORING: after EVERY finished run (aborts/crashes included) the bot
# posts ONE Discord embed summarizing ✅/❌ per group + all-time published
# count per page. No bot token needed — channel webhook only (set
# AP_DISCORD_WEBHOOK_URL in .env; see .env.example MONITORING block).
# Ledger = .local-capture/results/ledger.jsonl (gitignored, JSONL):
poetry run python -m poster.notify --test    # send a fake summary, verify webhook
poetry run python -m poster.notify --stats   # all-time published count per group
# GOTCHA: Discord/Cloudflare 403s (error 1010) urllib's default User-Agent —
# poster/notify.py sends a custom one; never "simplify" that header away.
```

Convenience (from `setup.sh`, registered in the shell rc): commands live in
`shell/autoposter.sh` (bash + zsh compatible; rc file gets a single source
line, repo path self-resolves). `ap-record [URL]` = the record command above
(no arg = bare browser, any site; `AP_RECORD_PROFILE=<dir>` overrides the
profile); `ap-timeline [dir] [--full]` = newest dump under
`.local-capture/manual_session/`; `ap-status` = `poster.main --status`.

Recorder console: `s`+Enter screenshot+note, `q`+Enter quit & flush.
Submodule gotchas: see `scraping_recorder/AGENTS.md` (poetry-install warning is
harmless; use `poetry run python -m pytest`, not PATH pytest).

## Posting rules (user spec — non-negotiable)

1. ONE post per group per run; content = `data/post.txt` copied 1:1 (no reformat).
2. Photos `data/car_photos/NN_car/` — folders sorted = car order in post.txt,
   files sorted inside each car; upload one batch per car folder.
3. OS file dialog: never let it open — `page.on("filechooser")` →
   `fc.set_files(...)`; fallback `set_input_files` on `input[type=file]`.
4. `posting_code` in groups.json = NAME OF THAT GROUP'S FLOW FUNCTION
   (`poster/flows.py::<code>(page, post)`). Unknown code = hard error, never guess.
5. Random `uniform(AP_DELAY_MIN, AP_DELAY_MAX)` sleep before every post /
   group / URL / page action; per-char typing jitter. USER RULE: waits must be
   short — 3-7s, and the loader hard-caps any config at 7s (never longer).
6. DEV SAFETY: never post or comment unless the selector is proven against a
   recording/dry-run. `AP_DRY_RUN=true` is default and fail-safe.
7. MONITORING: exactly ONE Discord summary per run, sent only after it ends
   (success, failure, abort, crash — all notify). Dry-run attempts record as
   `staged` and NEVER count as published. A webhook failure never changes
   the run's exit code (`poster/notify.py` swallows everything).

## Protocol facts (from recording 2026-09-17, group 249803862915566 Cuauhtémoc)

- Login lands on MAIN account (c_user=61592579496197); Carmazon
  i_user/av=61592323007979 — must switch. UI is Spanish: composer trigger
  `Escribe algo...`, publish `Publicar`. Match text/aria, never obfuscated classes.
- Profile switch (verified): click `[aria-label="Tu perfil"]` (no img child),
  stamp the Carmazon menu row `data-ap-switch`, then Playwright locator
  `.click()` — JS-dispatched `.click()` and raw `mouse.click(x,y)` both fail
  (untrusted / no auto-scroll).
- Posts go through group admin approval → verify via `/groups/<id>/my_pending_content/`.
- Photo attach PROVEN (recording 20260917T063422Z): composer dialog holds a
  hidden `input[type=file]`; picking a file POSTs to
  `upload.facebook.com/ajax/react_composer/attachments/photo/upload`.
  Bot answers the OS dialog via set_input_files / FileChooser — never clicks through it.

## Group pipeline workflow (the index)

ap-record → implement → verify is tracked automatically (`poster/groups_index.py`,
alias `ap-status` / `--status`): status is COMPUTED from three sources
(dumps on disk, REGISTRY functions, per-machine `data/dryrun_ok.json` stamp
written after a successful dry run), never hand-declared. When wiring a new
group: add its entry to `data/groups.json` with a `posting_code` — until the
flow function exists the index shows it as a GAP, so nothing can be forgotten
between recording and implementation.

## Conventions

- All knobs in `.env` (`AP_` prefix, see `.env.example`); `.env`,
  `.local-capture/**`, and `data/car_photos/**` images are gitignored.
- Roadmap: photo recording → monitoring/Discord summary ✅ (done 2026-09-17:
  poster/results.py + poster/notify.py) → more groups (record → new flow fn →
  groups.json) → scheduler + delivery verification (parse my_pending_content
  so "published" can mean admin-approved).
