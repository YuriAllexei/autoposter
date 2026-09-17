# autoposer — Agent Guide

Automated posting for Facebook Groups to run a car distribution sale.
Workflow: record a human-driven session on a target group with
`scraping_recorder` (git submodule), reverse-engineer the post-publish
protocol from the dump, then replay it headlessly with a persistent profile.

## Layout

```
autoposer/
├── agent.md               # this file
└── scraping_recorder/     # git submodule: https://github.com/YuriAllexei/scraping_recorder.git
    ├── record_session.py  # the recorder (human-in-the-loop, headed Firefox)
    ├── session_timeline.py# merges actions+HTTP+WS into one time-ordered stream
    ├── lifecycle_capture.py
    └── ws_smoke.py        # local echo WS server for offline self-test
```

## Environment (verified on this host)

- WSL + WSLg: headed browsers display fine (`DISPLAY=:0` present).
- Python 3.14 system python already has the `playwright` package
  (`~/.local/lib/python3.14/...`) and browsers in `~/.cache/ms-playwright`
  (firefox-1538/1543). No venv/poetry needed to run the recorder:
  `python3 scraping_recorder/record_session.py ...` works as-is.
- Recorder requires Python >=3.12 (pyproject). Pure stdlib + Playwright.

## Recording a session

From the submodule dir (dumps land in `scraping_recorder/.local-capture/`,
which is gitignored — NEVER commit a dump; it contains cookies/typed data):

```bash
cd scraping_recorder
python3 record_session.py \
  --url https://www.facebook.com/groups/<GROUP_ID> \
  --profile .local-capture/profiles/facebook \   # persistent: keeps FB login
  --trace                                        # optional but recommended
```

Interactive console while it runs (in the terminal, needs Enter):
- `s` + Enter — screenshot the page, then type an annotation note
- `q` + Enter — quit and flush the dump (dump dir path printed at exit)
- Closing the browser window or Ctrl-C also finishes.

Output: `<out>/<timestamp>_<host>/` with
`manual_actions.jsonl` (clicks/inputs/keys with css selector, xpath, text,
coordinates), `manual_requests.jsonl` (HTTP req/resp bodies, redacted keys,
20KB body cap), `ws_frames.jsonl` (verbatim WS frames both directions —
Facebook chats/notifications ride on WS), `summary.json`, `shots/`.

## Reviewing a dump

```bash
python3 scraping_recorder/session_timeline.py <session_dir> [--full]
```

Read the timeline FIRST, then raw JSONL. For Facebook the interesting traffic
is usually a `POST /api/graphql/` (or `/ajax/...`) with `fb_dtsg`/`jazoest`
anti-CSRF tokens; bodies are capped at 20k chars — if a request body is
truncated, re-derive it from the trace (`npx playwright show-trace trace.zip`)
rather than guessing.

## Facebook-specific notes

- Login wall: log in once inside the recorder with `--profile
  .local-capture/profiles/facebook`; reuse that profile for every later
  session and for the automation itself. Never store the password in the repo;
  the browser profile holds the session cookie.
- Record the FULL post flow as the acceptance target for automation:
  open composer → write text → attach photo(s) → select audience (Public vs
  group) → Publish → verify the post appears in the group feed. Screenshot +
  annotate each of these steps (`s` + note in the console) so selectors can be
  cross-checked against ground truth.
- FB selectors are obfuscated/have unstable class names. Prefer `data-*`
  attributes, aria-labels, and visible text (e.g. the composer placeholder
  "What's on your mind?") over generated class chains; record xpath too.
- 2FA/one-time codes: enter them manually during the recording session.

## Protocol facts learned from recording `20260917T014025Z_www.facebook.com`

- Target group: **VENTAS DE CARROS CUAUHTÉMOC 💥 CHIHUAHUA** —
  `https://www.facebook.com/groups/249803862915566` (id `249803862915566`).
- Posting account: professional profile **"Carmazon"** (`__user`/`av` =
  `61592323007979`).
- UI language: **Spanish** — composer trigger `Escribe algo...`, publish
  button `Publicar`, composer close `Cerrar el cuadro de diálogo del editor`.
  Match on text/aria, not obfuscated classes (`x1i10hfl` etc.).
- Post created via POST `https://www.facebook.com/api/graphql/`,
  `fb_api_req_friendly_name=ComposerStoryCreateMutation`,
  `doc_id=28594846586871901` (ids rotate — DOM replay preferred over raw
  replay). `variables.input` carries `message.text`,
  `composer_type:"group"`, `renderLocation:"group"`, `isGroup:true`.
  Auth params on every request: `fb_dtsg`, `jazoest`, `lsd`, `av`, `__user`
  (session-bound, must be harvested live from the page).
- Group posts go through **admin approval** — check
  `/groups/<id>/my_pending_content/`. The test post from the recording has NO
  delete mutation in the wire log (the `Eliminar` click never sent one), so it
  is likely still pending — verify/clean up manually.
- Photo flow NOT captured: no `rupload`/photo-upload requests in the session
  (test post was text-only). A second recording WITH a real photo attached is
  required before implementing photo posts; Playwright `set_input_files` on
  `input[type=file]` is the likely automation path.
- Login in that profile hit `two_step_verification` + reCAPTCHA — do NOT
  automate credential entry; rely on the persistent-profile session.
- That recording ran WITHOUT `--profile`, so its login was throwaway. Fixed:
  probe/automation use the persistent profile (below).

## Dry-run probe (no posting)

`scripts/probe_group.py` — opens headed Firefox on the persistent profile
(`.local-capture/profiles/facebook`); if not logged in, you log in once by
hand in that window (password never stored in code/git); the script waits,
then read-only locates the posting UI (composer trigger / contenteditable
boxes / photo buttons / file inputs / publish buttons) and dumps a JSON
report + screenshot to `.local-capture/probe/`. Never clicks. Run:

```bash
python3 scripts/probe_group.py            # defaults to the Cuauhtémoc group
```

## Automation roadmap (target)

1. ~~Record a manual session~~ DONE for text posts (recording above).
   Pending: record one WITH a real photo; re-record is cheap thanks to
   `--profile` persisting the login.
2. `poster/`: replay layer — Playwright persistent-context script taking
   {group_url, text, photos[]} → click `Escribe algo...` → type into the
   dialog textbox → `set_input_files` for photos → click `Publicar`. Dry-run
   mode MUST never click publish. DOM replay (not raw graphql).
3. Sale inventory source (car listings: JSON/CSV) → per-car post content
   (title, price, year, mileage, photos, contact line).
4. Scheduler + rate limiting (FB flags rapid group posting; human-ish
   delays, one group at a time initially) + per-post logging; verify delivery
   via `my_pending_content` / feed read-back.

## Conventions

- Submodule pointer: bump intentionally only — `git -C scraping_recorder pull`
  then `git add scraping_recorder` in the superproject.
- Dumps, profiles, and anything under `.local-capture/` stay out of git.
- Tests in the submodule: `python3 -m pytest scraping_recorder/tests -q`.
