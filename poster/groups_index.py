"""Group lifecycle index — tracks recorded -> implemented -> verified -> live.

data/groups.json is the single source of truth (no parallel file). Each group
entry gains:
  "recording": path of the ap-record dump this flow was built from (or null)

Pipeline states (computed, not trusted from typing):
  recorded          a dump exists on disk mentioning the group id
  implemented       posting_code resolves to a real function in flows.REGISTRY
  dryrun_verified   a dry run SUCCEEDED and poster.main stamped data/dryrun_ok.json
  live              AP_DRY_RUN=false in .env (user's call)

`render_report()` + gaps are what `poster.main --status` / `ap-status` prints:
recordings nobody wired up, entries whose flow fn is missing, and
implemented-but-never-dry-run — the "what's left" list.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

GROUP_ID_RE = re.compile(r"facebook\.com/groups/(\d+)")
REPO = Path(__file__).resolve().parent.parent
CAPTURE_ROOT = REPO / ".local-capture/manual_session"
MARKER_FILE = REPO / "data/dryrun_ok.json"


def group_ids_in_text(text: str) -> set[str]:
    return set(GROUP_ID_RE.findall(text or ""))


VANITY_SLUG_RE = re.compile(r"facebook\.com/groups/([A-Za-z][A-Za-z0-9._-]{2,})/?")


def resolve_vanity_group_id(url: str, dump_dir: Path) -> set[str]:
    """Vanity-slug start URL (/groups/CarrosBaratoss/) -> numeric group id.

    Some groups are only ever browsed by vanity URL, so the plain id regex
    never fires and the recording would silently land in the untagged bucket.
    The dump itself holds the proof: FB's captured route-definition responses
    carry "groupID":"<digits>" in the same JSON as the slug route. Require
    co-occurrence in a small window around the match so unrelated group ids
    from feed data can't leak into the mapping.
    """
    m = VANITY_SLUG_RE.search(url or "")
    if not m:
        return set()
    slug = m.group(1)
    requests_file = dump_dir / "manual_requests.jsonl"
    if not requests_file.exists():
        return set()
    hits: dict[str, int] = {}
    try:
        with requests_file.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if slug not in line:
                    continue
                try:
                    body = json.loads(line).get("response_body") or ""
                except json.JSONDecodeError:
                    continue
                body = json.dumps(body) if isinstance(body, dict) else str(body)
                if slug not in body:
                    continue
                for g in re.finditer(r'"groupID":"(\d{8,16})"', body):
                    if slug in body[max(0, g.start() - 3000):g.end()]:
                        hits[g.group(1)] = hits.get(g.group(1), 0) + 1
    except OSError:
        return set()
    return {max(hits, key=lambda k: hits[k])} if hits else set()


def scan_recordings(capture_root: Path = CAPTURE_ROOT) -> dict[str, list[dict]]:
    """group_id -> recording summaries found on disk, newest first.

    Reads every */summary.json under the capture root (handles both the flat
    and <site>/<stamp> layouts). The group id may appear in the start URL or
    anywhere in the purpose/annotation text.
    """
    found: dict[str, list[dict]] = {}
    if not capture_root.exists():
        return found
    for sj in capture_root.rglob("summary.json"):
        try:
            data = json.loads(sj.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        blob = " ".join(str(data.get(k, "")) for k in ("url", "purpose", "dir", "site"))
        ids = group_ids_in_text(blob)
        if not ids:  # vanity-slug URL: resolve via the dump's own captured traffic
            ids = resolve_vanity_group_id(str(data.get("url", "")), sj.parent)
        rec = {
            "dir": str(sj.parent.relative_to(capture_root.parent)),
            "ts": sj.parent.name,
            "url": data.get("url", ""),
            "purpose": (data.get("purpose") or "")[:80],
        }
        if not ids:  # recording not tied to any group (e.g. login-only session)
            found.setdefault("", []).append(rec)
            continue
        for gid in ids:
            found.setdefault(gid, []).append(rec)
    for v in found.values():
        v.sort(key=lambda r: r["ts"], reverse=True)
    return found


def load_groups(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("groups", [])


def load_markers(path: Path = MARKER_FILE) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def stamp_dryrun_ok(group_url: str, evidence: str,
                    path: Path = MARKER_FILE) -> dict:
    """Called by poster.main after a SUCCESSFUL dry run (composer staged+closed)."""
    gid = (group_ids_in_text(group_url) or {""}).pop()
    if not gid:
        return {}
    markers = load_markers(path)
    markers[gid] = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "evidence": evidence,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(markers, indent=2, ensure_ascii=False),
                    encoding="utf-8")
    return markers[gid]


def compute_status(groups: list[dict], registry_names: set[str],
                   markers: dict, recordings: dict[str, list[dict]]) -> list[dict]:
    """Per-entry row of truth: declared intent vs actually computed state."""
    rows = []
    for g in groups:
        gid = (group_ids_in_text(g.get("group_url", "")) or {""}).pop()
        code = g.get("posting_code", "")
        implemented = code in registry_names
        marker = markers.get(gid)
        rec = (g.get("recording")
               or (recordings.get(gid, [{}])[0]["dir"] if recordings.get(gid) else ""))
        rows.append({
            "id": gid,
            "name": g.get("name", ""),
            "code": code,
            "enabled": g.get("enabled", True),
            "recording": rec or None,
            "implemented": implemented,
            "dryrun": marker,
            "stage": ("live-ready (dryrun verified)" if marker and implemented
                      else "implemented, dry-run pending" if implemented
                      else "recorded only, flow missing" if rec
                      else "entry only — nothing on disk"),
        })
    return rows


def find_gaps(groups: list[dict], recordings: dict, registry_names: set) -> list[str]:
    """Actionable mismatches — things the user said we should 'know somehow'."""
    gaps = []
    listed_ids = set()
    for g in groups:
        listed_ids |= group_ids_in_text(g.get("group_url", ""))
    for gid, recs in sorted(recordings.items()):
        if gid and gid not in listed_ids:
            gaps.append(f"recording {recs[0]['dir']} (group {gid}) has NO entry in "
                        f"data/groups.json")
    for g in groups:
        gid = (group_ids_in_text(g.get("group_url", "")) or {"?"}).pop()
        code = g.get("posting_code", "")
        if gid and code and code not in registry_names:
            gaps.append(f"group {gid} ({g.get('name','')}) posting_code '{code}' "
                        f"has NO function in poster/flows.py REGISTRY")
    return gaps


def render_report(rows: list[dict], gaps: list[str], untagged: list[dict],
                  cfg) -> str:
    out = ["", "GROUP INDEX  (source of truth: data/groups.json)", ""]
    out.append(f"  {'group id':<17} {'stage':<28} name / evidence")
    out.append("  " + "-" * 96)
    for r in rows:
        ev = []
        ev.append(f"rec:{r['recording'].split('/')[-1][:28]}" if r["recording"]
                  else "rec:NONE")
        ev.append(f"code:{r['code']}" if r["implemented"]
                  else f"code:{r['code']} MISSING")
        ev.append(f"dryrun:{r['dryrun']['ts'][:19]}" if r["dryrun"]
                  else "dryrun:not verified")
        if not r["enabled"]:
            ev.append("(disabled)")
        out.append(f"  {r['id'] or '?':<17} {r['stage']:<28} {r['name'][:30]}")
        out.append(f"  {'':<17} {'':<28} {' | '.join(ev)}")
    if untagged:
        out.append("\nRecordings without a group URL (login/exploration sessions):")
        for rec in untagged[:6]:
            out.append(f"  {rec['dir']}  ({rec['purpose'] or rec['url'][:60]})")
    out.append("\nGAPS — recorded-but-not-wired / wired-but-broken:")
    if gaps:
        out.extend(f"  !! {g}" for g in gaps)
    else:
        out.append("  none — every recording is in groups.json and every "
                   "posting_code resolves to a flow function")
    mode = "LIVE (AP_DRY_RUN=false)" if not cfg.dry_run else "dry-run"
    out.append(f"\nMode: {mode} | flows in REGISTRY: "
               f"{', '.join(sorted(_registry_names())) or '(none)'}")
    return "\n".join(out)


def _registry_names() -> set[str]:
    from . import flows  # local import: avoid cycles in unit tests
    return set(flows.REGISTRY)


def status_report(cfg) -> str:
    recordings = scan_recordings()
    rows = compute_status(load_groups(cfg.groups_file), _registry_names(),
                          load_markers(), recordings)
    gaps = find_gaps(load_groups(cfg.groups_file), recordings, _registry_names())
    return render_report(rows, gaps, recordings.get("", []), cfg)
