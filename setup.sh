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
  # Non-fatal: a download hiccup must not abort setup before the shell
  # commands are registered (that left teammates with no ap-* commands).
  poetry run playwright install firefox || \
    echo "   !! firefox download failed — rerun later: poetry run playwright install firefox" >&2
fi

if [[ "$WITH_ALIAS" == 1 ]]; then
  echo "==> registering ap-* commands in ${ALIAS_FILE} (helper: shell/autoposter.sh)"
  MARKER="# >>> autoposter >>>"
  # replace existing block in place, else append
  python3 - "$ALIAS_FILE" "$MARKER" "${REPO}/shell/autoposter.sh" <<'PYEOF'
import pathlib, sys
rc_path, marker, helper = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
s = rc_path.read_text() if rc_path.exists() else ""
end_marker = "# <<< autoposter <<<"
block = marker + "\n. " + helper + "\n" + end_marker
start, end = s.find(marker), s.find(end_marker)
if start != -1 and end != -1:
    s = s[:start] + block + s[end + len(end_marker):]
else:
    s = s.rstrip("\n") + "\n\n" + block + "\n"
rc_path.write_text(s)
PYEOF
  # prove the helper actually defines all three commands in BOTH shells
  for sh in bash zsh; do
    if command -v "$sh" >/dev/null; then
      "$sh" -c ". '${REPO}/shell/autoposter.sh' && type ap-record ap-timeline ap-status >/dev/null" \
        && echo "    ${sh}: ap-record/ap-timeline/ap-status OK" \
        || { echo "    ${sh}: FAILED to load shell/autoposter.sh" >&2; exit 1; }
    fi
  done
fi

if [[ ! -f .env ]]; then
  echo "==> creating .env from .env.example (edit values before real runs)"
  cp .env.example .env
else
  echo "==> .env exists, leaving it alone"
fi

mkdir -p .local-capture/profiles

cat <<EOF

Done. Next steps on a fresh machine:
  1. cp .env  ->  edit it:   FB IDs (see comments), AP_DISCORD_WEBHOOK_URL
     (channel webhook — see .env.example), profile/paths if non-default.
  2. source ${ALIAS_FILE}
  3. ap-record            # headed Firefox; log into FB BY HAND the first time
                          # (2FA included — cookies persist in the profile).
                          # then record whatever group flow you want.
                          # 'q'+Enter saves the dump + asks purpose/label.
  4. ap-status            # group index: recorded / implemented / dry-run /
                          # gaps (recordings not yet wired into groups.json)
  5. ap-timeline --full   # time-ordered review of the newest dump
EOF
