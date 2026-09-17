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

## Automation roadmap (target, not yet built)

1. Recorded session dump of one group post (this step).
2. `autoposer/poster/`: replay layer — Playwright persistent-context script
   taking {group_url, text, photos[]} → performs the post via DOM actions
   (recommended over raw graphql replay; FB tokens expire quickly).
3. Sale inventory source (car listings: JSON/CSV) → per-car post content.
4. Scheduler + rate limiting (FB flags rapid group posting; keep human-ish
   delays, one group at a time initially).

## Conventions

- Submodule pointer: bump intentionally only — `git -C scraping_recorder pull`
  then `git add scraping_recorder` in the superproject.
- Dumps, profiles, and anything under `.local-capture/` stay out of git.
- Tests in the submodule: `python3 -m pytest scraping_recorder/tests -q`.
