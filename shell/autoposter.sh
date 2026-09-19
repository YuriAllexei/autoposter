# autoposter shell integration — sourced from ~/.bashrc or ~/.zshrc.
# Defines: ap-record, ap-timeline, ap-status.  bash + zsh compatible.
# Repo path resolves from THIS file's location, so moving/cloning the repo
# needs no rc edits beyond the single source line setup.sh installs.
_AP_SELF="${BASH_SOURCE:-}"
[[ -z "$_AP_SELF" ]] && _AP_SELF="${(%):-%x}"   # zsh fallback
_AP_REPO="$(cd "$(dirname "$_AP_SELF")/.." && pwd)"
unset _AP_SELF
# ap-record [URL] [flags] — record a manual browser session (any site).
# No URL = bare browser. Dumps: <repo>/.local-capture/manual_session/
ap-record() {
  local extra=()
  [[ -n "$1" && "$1" != -* ]] && { extra=(--url "$1"); shift; }
  cd "$_AP_REPO" && poetry run python scraping_recorder/record_session.py \
    "${extra[@]}" --profile "${AP_RECORD_PROFILE:-.local-capture/profiles/facebook}" \
    --out .local-capture/manual_session --trace "$@"
}
# ap-timeline [dump-dir] [--full] — review a dump (default: newest)
ap-timeline() {
  local dump="$1"; shift
  [[ "$dump" == -* || -f "$dump/summary.json" ]] || { [[ -d "$dump" ]] && dump="$dump/$(ls -1dt "$dump"/*/ 2>/dev/null | head -1)"; }
  [[ "$dump" == -* ]] && { set -- "$dump"; dump=""; }
  [[ -z "$dump" || ! -f "$dump/summary.json" ]] && dump=$(find "$_AP_REPO/.local-capture/manual_session" -maxdepth 3 -name summary.json -printf '%T@ %h\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)
  [[ -z "$dump" ]] && { echo "no dumps in .local-capture/manual_session yet"; return 1; }
  echo "reviewing: $dump"
  cd "$_AP_REPO" && poetry run python scraping_recorder/session_timeline.py "$dump" "$@"
}
# ap-status — group index: recorded / implemented / dry-run-verified + gaps
ap-status() {
  cd "$_AP_REPO" && poetry run python -m poster.main --status
}
