# autoposter — Agent Guide

Facebook group auto-posting bot (Playwright + persistent Firefox profile).
Posts our car-inventory summary as the configured identity (personal profile
"Carmazon Alex" or page "Carmazon") into every group the identity has
joined — the target list is FETCHED LIVE each run (poster/groups_fetch.py),
not a hand-kept file. Flows come from human recordings via the
`scraping_recorder` submodule but are keyed by composer LAYOUT, not by
group: one flow serves every group with that layout (2026-09-22 diff of all
recordings: all Spanish groups share one identical composer).

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
poetry run python -m poster.main --group 249803862915566   # only this joined group
# --live only works when .env already has AP_DRY_RUN=false (fail-safe by design)
poetry run python -m poster.main --live

# TARGETS ARE LIVE: each run fetches the identity's joined groups
# (GroupsCometJoinsRootQuery + its pagination doc — see
# poster/groups_fetch.py) and rotates: never-attempted first (FB joins
# order), then oldest last-attempt from the ledger; cap
# AP_MAX_POSTS_PER_RUN. THE only per-group gate = composer trigger
# renders within 5s, else status `skipped` (posting not enabled there).
# ap-groups = same fetch, print-only rotation report (no browser left open
# after; read-only):
poetry run python -m poster.main --list-groups  # alias: ap-groups

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
`.local-capture/manual_session/`; `ap-groups` = `poster.main --list-groups`.

Recorder console: `s`+Enter screenshot+note, `q`+Enter quit & flush.
Submodule gotchas: see `scraping_recorder/AGENTS.md` (poetry-install warning is
harmless; use `poetry run python -m pytest`, not PATH pytest).

## Posting rules (user spec — non-negotiable)

1. ONE post per group per run; content = `data/post.txt` copied 1:1 (no reformat).
2. Photos `data/car_photos/NN_car/` — folders sorted = car order in post.txt,
   files sorted inside each car; upload one batch per car folder.
3. OS file dialog: never let it open — `page.on("filechooser")` →
   `fc.set_files(...)`; fallback `set_input_files` on `input[type=file]`.
4. TARGETS = the identity's JOINED groups fetched live each run
   (poster/groups_fetch.py, [proven recording 20260922T214219Z]); rotation =
   never-attempted first then oldest-ledger-attempt, capped
   AP_MAX_POSTS_PER_RUN. Sole gate: the 'Escribe algo...' composer trigger
   must RENDER within 5s of the group page, else the group is `skipped`
   (posting not enabled for our identity — never treated as an error).
   All Spanish layouts share ONE flow (group_composer_es_v1); a layout that
   actually differs needs a recording + new flow fn first — never guess.
   If the joins fetch itself fails, the run ABORTS (exit 4 + Discord) — a
   broken fetch must never mean "post to nothing".
5. Random `uniform(AP_DELAY_MIN, AP_DELAY_MAX)` sleep before sensitive steps
   (first group open, before publish). USER RULE: general waits short — 2-4s
   (loader hard-caps config at 7s, never longer). GROUP CHANGE is its own
   category: uniform 10-15s (`AP_GROUP_SWITCH_MIN/MAX_SECONDS`, own ceiling
   20s) after finishing one group, before opening the next — no sleep after
   the last group. Eliminated 2026-09-22: NO sleep between photo-batch
   attachments (settling = `_wait_upload_settled` condition-wait) and post
   text is PASTED via `execCommand('insertText')` (Enter per line) with the
   1:1 read-back gate — char-by-char keyboard.type survives only as one-shot
   fallback.
6. DEV SAFETY: never post or comment unless the selector is proven against a
   recording/dry-run. `AP_DRY_RUN=true` is default and fail-safe.
7. MONITORING: exactly ONE Discord summary per run, sent only after it ends
   (success, failure, abort, crash — all notify). Dry-run attempts record as
   `staged` and NEVER count as published; no-composer groups record as
   `skipped` (⏭️ in the embed). A webhook failure never changes the run's
   exit code (`poster/notify.py` swallows everything).

## Protocol facts (from recording 2026-09-17, group 249803862915566 Cuauhtémoc)

- Login lands on MAIN account (c_user=61592579496197); Carmazon
  i_user/av=61592323007979 — must switch. UI is Spanish: composer trigger
  `Escribe algo...`, publish `Publicar`. Match text/aria, never obfuscated classes.
- Identity MODES (`AP_POST_AS`), recording 20260922T210535Z (profile_switcher):
  `page` = post as Carmazon; `profile` = post as the personal login. The
  acting identity is the `av` param on /api/graphql requests — av=61592323007979
  (Carmazon) vs av=61592579496197 (personal, == c_user). i_user cookie exists
  ONLY for the page and CANNOT distinguish the personal profile switch.
  The account-menu switcher lists BOTH rows in either state, and the personal
  row text is "Carmazon Alex" (NOT the legal name "MrAlexei Villa") —
  `AP_FB_MAIN_PROFILE_NAME`. 'Carmazon' is a substring of 'Carmazon Alex', so
  the row matcher prefers EXACT text matches (CLICK_PROFILE_ITEM_JS). Group
  composer flow is IDENTICAL in both modes.
- Profile switch (verified): click `[aria-label="Tu perfil"]` (no img child),
  stamp the Carmazon menu row `data-ap-switch`, then Playwright locator
  `.click()` — JS-dispatched `.click()` and raw `mouse.click(x,y)` both fail
  (untrusted / no auto-scroll).
- Posts go through group admin approval → verify via `/groups/<id>/my_pending_content/`.
- Photo attach PROVEN (recording 20260917T063422Z): composer dialog holds a
  hidden `input[type=file]`; picking a file POSTs to
  `upload.facebook.com/ajax/react_composer/attachments/photo/upload`.
  Bot answers the OS dialog via set_input_files / FileChooser — never clicks through it.

## Adding groups (there is no file to edit anymore)

Join the group with the posting identity in the real browser — the next run
picks it up automatically from the live joins fetch (order = FB's
viewer_added sort). `ap-groups` shows every joined group + its rotation
state. Recordings are ONLY for an unseen composer LAYOUT (different
language UI, rules/questions gate, or a flow failing on proven selectors) —
not per group. If the joins fetch ever fails (doc_id churn), the run aborts
loudly (exit 4 + Discord); it never silently posts to "nothing".

## Conventions

- All knobs in `.env` (`AP_` prefix, see `.env.example`); `.env`,
  `.local-capture/**`, and `data/car_photos/**` images are gitignored.
- Roadmap: photo recording → monitoring/Discord summary ✅ → flow collapse
  (2026-09-22: one Spanish composer serves all) ✅ → dynamic targets +
  rotation ✅ (2026-09-22: groups.json/ap-status deleted; joined list
  fetched live each run, ledger rotates) → scheduler + delivery
  verification (parse my_pending_content
  so "published" can mean admin-approved).
