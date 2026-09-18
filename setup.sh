#!/usr/bin/env bash
# autoposter setup — one-shot dev environment bootstrap.
# Usage: ./setup.sh [--no-browser] [--no-alias] [--alias-file FILE]
set -euo pipefail
cd "$(dirname "$0")"

REPO="$(pwd)"
WITH_BROWSER=1
WITH_ALIAS=1
# pick rc file for the user's login shell (zsh users sourcing .bashrc breaks: shopt etc.)
if [[ -n "${ALIAS_FILE:-}" ]]; then :
elif [[ "$(basename "${SHELL:-bash}")" == "zsh" ]]; then ALIAS_FILE="${HOME}/.zshrc"
else ALIAS_FILE="${HOME}/.bashrc"; fi

usage() { grep '^# ' "$0" | sed 's/^# //'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-browser)  WITH_BROWSER=0 ;;
    --no-alias)    WITH_ALIAS=0 ;;
    --alias-file)  ALIAS_FILE="$2"; shift ;;
    -h|--help)     usage; exit 0 ;;
    *) echo "unknown flag: $1 (see --help)" >&2; exit 1 ;;
  esac
  shift
done

command -v poetry >/dev/null || { echo "poetry not found — install: pipx install poetry" >&2; exit 1; }

echo "==> git submodules (scraping_recorder)"
git submodule update --init --recursive

echo "==> poetry env (install deps from pyproject.toml + poetry.lock)"
poetry install

if [[ "$WITH_BROWSER" == 1 ]]; then
  echo "==> playwright firefox (skips download if already present)"
  poetry run playwright install firefox
fi

if [[ ! -f .env ]]; then
  echo "==> creating .env from .env.example (edit values before real runs)"
  cp .env.example .env
else
  echo "==> .env exists, leaving it alone"
fi

mkdir -p .local-capture/profiles

if [[ "$WITH_ALIAS" == 1 ]]; then
  echo "==> registering ap-record + ap-timeline in ${ALIAS_FILE}"
  BLOCK="$(mktemp)"
  cat > "$BLOCK" <<'EOF'
# >>> autoposter >>>
# ap-record [URL] [extra recorder flags] — record a manual browser session.
# Bare browser with no URL; captures everything you do, any site.
# Profile dir override: AP_RECORD_PROFILE=<dir> (default: the bot's FB profile).
# q+Enter saves + prompts for purpose/label.
# Dumps: .local-capture/manual_session/<site>/<stamp>[_label]/
ap-record() {
  local extra=()
  [[ -n "$1" && "$1" != -* ]] && { extra=(--url "$1"); shift; }
  cd /REPO_PATH && poetry run python scraping_recorder/record_session.py \
    "${extra[@]}" --profile "${AP_RECORD_PROFILE:-.local-capture/profiles/facebook}" \
    --out .local-capture/manual_session --trace "$@"
}
# ap-timeline [dump-dir] [--full] — review dump (default: newest by summary.json)
ap-timeline() {
  local dump="$1"; shift
  [[ "$dump" == -* || -f "$dump/summary.json" ]] || { [[ -d "$dump" ]] && dump="$dump/$(ls -1dt "$dump"/*/ 2>/dev/null | head -1)"; }
  [[ "$dump" == -* ]] && { set -- "$dump"; dump=""; }
  [[ -z "$dump" || ! -f "$dump/summary.json" ]] && dump=$(find /REPO_PATH/.local-capture/manual_session -maxdepth 3 -name summary.json -printf '%T@ %h\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)
  [[ -z "$dump" ]] && { echo "no dumps in .local-capture/manual_session yet"; return 1; }
  echo "reviewing: $dump"
  cd /REPO_PATH && poetry run python scraping_recorder/session_timeline.py "$dump" "$@"
}
# ap-status — group implementation index: recorded / implemented / dry-run
# verified, plus gaps (recordings not wired into groups.json, missing fns).
ap-status() {
  cd /REPO_PATH && poetry run python -m poster.main --status
}
# <<< autoposter <<<
EOF
  sed -i "s|/REPO_PATH|${REPO}|g" "$BLOCK"
  # replace an existing block in place, else append (idempotent re-runs)
  python3 - "$ALIAS_FILE" "$BLOCK" <<'PYEOF'
import pathlib, sys
rc, block = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]).read_text()
s = rc.read_text() if rc.exists() else ""
start, end = s.find("# >>> autoposter >>>"), s.find("# <<< autoposter <<<")
if start != -1 and end != -1:
    s = s[:start] + block.rstrip("\n") + s[end + len("# <<< autoposter <<<"):]
else:
    s = s.rstrip("\n") + "\n\n" + block
rc.write_text(s)
PYEOF
  rm -f "$BLOCK"
fi

cat <<EOF

Done. Next steps:
  1. source ${ALIAS_FILE}
  2. ap-record 249803862915566        # opens headed Firefox; log in by hand
                                        first run, then do the flow you want
                                        captured (composer -> text -> PHOTOS ->
                                        Publicar). 'q'+Enter saves the dump.
  3. ap-timeline --full               # time-ordered review of the dump
EOF
