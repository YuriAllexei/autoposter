# autoposter shell integration — sourced from ~/.bashrc or ~/.zshrc.
# Defines: ap-record, ap-timeline, ap-groups, ap-gui.  bash + zsh compatible.
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
# ap-groups — fetch the live joined-group list of the configured identity
# (opens the browser; read-only) + rotation state from the ledger
ap-groups() {
  cd "$_AP_REPO" && poetry run python -m poster.main --list-groups
}

# ap-gui [up|down|logs|run <cmd>|login|status] — the app via docker compose.
# Default (bare `ap-gui`): bring the stack up. Code + .env + data/post.txt +
# car_photos + the Firefox profile are BIND-MOUNTED: host edits apply live;
# rebuild only when a dependency changes (docker compose build). Reach the
# dashboard from your WINDOWS browser: http://localhost:8765/
ap-gui() {
  cd "$_AP_REPO" || return 1
  case "${1:-up}" in
    up)
      docker compose up -d &&
      echo "dashboard: http://localhost:8765/  (stop with: ap-gui down)" ;;
    down|stop) docker compose down ;;
    logs) shift; docker compose logs -f "$@" ;;
    status) docker compose ps ;;
    run) shift; docker compose run --rm autoposter "$@" ;;
    login) docker compose run --rm --profile login autoposter-login ;;
    -h|--help|help)
      cat <<'TXT'
ap-gui [up|down|logs|run <cmd>|login|status]   (docker compose front-end)
  up      default: start the stack -> http://localhost:8765/ (Windows browser)
  down    stop everything container-side
  logs    follow the dashboard/run logs
  run     one-off inside the container, e.g.
            ap-gui run python -m poster.main --dry-run
            ap-gui run python -m poster.crosspost --list
  login   one-time headed Firefox Facebook sign-in (shared profile)
  status  container states
TXT
      ;;
    *) echo "ap-gui: unknown subcommand '$1' — try: up down logs run login" >&2
       return 2 ;;
  esac
}
