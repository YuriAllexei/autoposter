# autoposter — Agent Guide

Facebook group auto-posting bot (Playwright + persistent Firefox profile).
Posts our car-inventory summary as the professional profile "Carmazon" into
groups listed in `data/groups.json`. Per-group layouts come from human
recordings via the `scraping_recorder` submodule; flows can differ per group.

## Environment / commands

- WSL + WSLg (headed browsers OK). **Poetry manages everything: run all Python
  via `poetry run ...`, never bare `python3`/`pip`.** Setup: `poetry install`
  (playwright + pytest/ruff); browsers: `poetry run playwright install firefox`.
- Login = cookies in the persistent profile at `.local-capture/profiles/facebook`
  (does NOT exist yet on this host — first headed run needs manual login).
  Passwords never in code/env.

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

poetry run pytest tests scraping_recorder/tests -q
```

Convenience (from `setup.sh`, registered in the shell rc):
`ap-record [URL]` = the record command above (no arg = bare browser, any site;
`AP_RECORD_PROFILE=<dir>` overrides the profile); `ap-timeline [dir] [--full]`
= newest dump under `.local-capture/manual_session/`.

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

## Protocol facts (from recording 2026-09-17, group 249803862915566 Cuauhtémoc)

- Login lands on MAIN account (c_user=61592579496197); Carmazon
  i_user/av=61592323007979 — must switch. UI is Spanish: composer trigger
  `Escribe algo...`, publish `Publicar`. Match text/aria, never obfuscated classes.
- Profile switch (verified): click `[aria-label="Tu perfil"]` (no img child),
  stamp the Carmazon menu row `data-ap-switch`, then Playwright locator
  `.click()` — JS-dispatched `.click()` and raw `mouse.click(x,y)` both fail
  (untrusted / no auto-scroll).
- Posts go through group admin approval → verify via `/groups/<id>/my_pending_content/`.
- Photo upload traffic NOT yet captured (only text recording exists).
  Required before real photo posts: record a session WITH photos.

## Conventions

- All knobs in `.env` (`AP_` prefix, see `.env.example`); `.env`,
  `.local-capture/**`, and `data/car_photos/**` images are gitignored.
- Roadmap: photo recording → `poster/` package (config, dispatcher,
  filechooser upload) → more groups (record → new flow fn → groups.json) →
  scheduler + delivery verification.
