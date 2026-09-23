# autoposter — Agent Guide

Facebook group auto-posting bot (Playwright + persistent Firefox profile).
Two posting pipelines + a control dashboard, all sharing one identity ritual:

- **TEXT POST** (`poster.main`): each run becomes the configured identity
  ("Carmazon Alex" profile / "Carmazon" page) → fetches joined groups LIVE →
  rotates (never-attempted first, then oldest ledger attempt, capped) → posts
  the car-inventory summary (data/post.txt + photos).
- **CROSSPOST** (`poster.crosspost`): share each active MARKETPLACE LISTING
  into every group the 'Publicar en más lugares' dialog offers — FB caps one
  submission at 20 groups, so a listing with 45 groups = batches 20/20/5,
  ~2 min apart, tracked IN THE RUN ONLY (user rule: no cross-run ledger
  memory for this pipeline).
- **DASHBOARD** (`poster.gui`): localhost page — tables (groups, listings,
  ledger) + one-click dry/live runs of either pipeline + log tail.

Flows come from human recordings via the `scraping_recorder` submodule and
are keyed by composer LAYOUT, not by group (all Spanish groups share one
identical composer).

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
# logged in · 3 identity switch failed · 4 joined-groups fetch failed
# (or --live refused without AP_DRY_RUN=false).

# CROSSPOST (marketplace listings → groups; same AP_DRY_RUN fail-safe —
# --live is REFUSED unless .env says AP_DRY_RUN=false):
poetry run python -m poster.crosspost --list    # cards + feed table, opens NO dialog
poetry run python -m poster.crosspost --dry-run # per batch: check boxes + screenshot, Cancelar
poetry run python -m poster.crosspost           # respects AP_DRY_RUN; all listings, all batches
poetry run python -m poster.crosspost --listing TAHOE --max 2   # filter listings
# rc: 0 ≥1 batch staged/published · 1 nothing to do · 2/3 identity · 4 --live refused.
# Ground truth: recording 20260923T021450Z_marketplace_article_fetching_and_mass_pu.

# DASHBOARD (localhost:8765, no auth — loopback bind is deliberate; one run
# at a time; a LIVE run additionally requires typing PUBLICAR + .env edit):
poetry run python -m poster.gui --open

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
`ap-record [URL]`, `ap-timeline [dir] [--full]`, `ap-groups`, `ap-gui`.
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
   after finishing one group, never after the last.
   CROSSPOST ACTIONS are their own category too (user spec 2026-09-23):
   1-3s between menu/dialog actions (`AP_CROSSPOST_ACTION_*`) and 0.2-1s
   micro-pauses BETWEEN consecutive group-checkbox clicks
   (`flows.CROSSPOST_ROW_CLICK_DELAY`, constant on purpose). NO sleep between photo
   batches (settling = `_wait_upload_settled` condition-wait). Post text is
   PASTED (`execCommand('insertText')`, Enter per line) behind the 1:1
   read-back gate; char-by-char typing survives only as one-shot fallback.
6. DEV SAFETY: never post or comment unless the selector is proven against a
   recording/dry-run. `AP_DRY_RUN=true` is default and fail-safe.
7. MONITORING: exactly ONE Discord summary per run, sent only after it ends
   (success/failure/abort/crash all notify). Dry-run stages never count as
   published; `skipped` never counts. Webhook failure never changes rc. A real publish carries a best-effort `delivered` verdict
  (pending|live|unknown) — see verify_pending; it never gates the status.
8. CROSSPOST TRACKING (user spec 2026-09-23): the batch plan is computed from
   the dialog's OWN group list each time it opens; the only memory is a set
   of already-batched group ids FOR THE CURRENT LISTING IN THE CURRENT RUN.
   Do NOT make run_listing read ledger/coverage across runs — the user
   rejected that design; ledger lines exist for the Discord summary and
   audit only.

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
- CROSSPOST dialog (recording 20260923T021450Z + live probe
  .local-capture/crosspost_probe/): every listing card = exactly one
  `[role=button][aria-label^='Más opciones para ']` → menuitem
  'Publicar en más lugares' → dialog 'En tus grupos'. The dialog's
  `MarketplaceCrossPostDialogQuery` payload (sniffed via expect_response)
  carries the eligible groups (id+name), `marketplace_crosspost_limit` (20)
  and the LISTING id — ids are not in the DOM. Group rows are
  `[role=dialog] [role=checkbox]`, text = '<NAME>\n<nn,n mil miembros>\n ·
  Público' → match folded FIRST LINE. The dialog is modal (aria-modal):
  close = no aria-modal [role=dialog] remains (`_DIALOG_GONE_JS`).
- RACE LESSONS (live 2026-09-23, cost two failed smokes): FB hydrates
  asynchronously everywhere — dialog rows appear AFTER their graphql
  response (skeleton first: wait for row count ≥ payload count), and the
  dialog FADES on Cancelar (condition-wait gone, never immediate count()).
  Same bug family as the listing '...' menu needing wait_for not count().
- MARKETPLACE LISTINGS feed (`poster/listings.py`,
  CometMarketplaceYouSellingFastContentContainerQuery): requires the
  `__relay_internal__pv__ShouldUpdate...relayprovider=false` provided
  variable (FB errors missing_required_variable_value without it — the
  JSONL recorder SCRUBBED its name, the trace did not). The feed only shows
  ACTIVE listings ("Todas las publicaciones") and the page renders each
  card TWICE → dedupe by folded title; the planned set = cards ∩ feed titles
  (USER RULE: 'Requieren atencion' cards are never cross-posted; if the feed
  breaks, degrade loudly to all-cards, never abort on metadata).
- PUBLISH step (recording 20260923T042928Z, captured LIVE): clicking
  'Publicar' fires `MarketplaceForSaleItemCreateXPostsMutation`
  doc_id=9628145373942390 (input: item_id, additional_target_ids=[group
  ids], actor_id, client_mutation_id, attribution_id_v2) and the dialog
  just CLOSES — no confirmation UI. Row clicks are TOGGLES (a double click
  nets to zero — never assume, read aria-checked back). Suggested-groups
  rows ('Grupos sugeridos') have no checkbox → unreachable by design.
- Crosspost reaches the groups the TEXT pipeline must skip: 'Vender algo'
  marketplace-tab groups still appear in the crosspost dialog.

## Adding groups

No file to edit: join with the posting identity in the real browser and the
next run picks it up from the live joins fetch (order = FB viewer_added).
`ap-groups` shows every joined group + rotation state. Recordings are ONLY
for an unseen composer LAYOUT, never per group.

## Conventions

- All knobs in `.env` (`AP_` prefix, see `.env.example`; real env always wins,
  `AP_ENV_FILE` relocates the file). `.env`, `.local-capture/**`, and
  `data/car_photos/**` images are gitignored.
- Delivery verification PARTIALLY landed: live publishes now run
  `verify_pending`, which checks my_pending_content then the group feed and
  stamps the ledger row `delivered: pending|live|unknown` (shown as the
  "live?" dashboard column + a Discord suffix). A failed verdict NEVER turns
  a publish into a failure. Still open: a scheduled re-check that flips an
  old "pending" to "live" once admins approve, and a cron scheduler.
