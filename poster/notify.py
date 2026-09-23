"""Discord monitoring: ONE summary embed per run, sent AFTER it finishes.

Webhook-based on purpose: no bot token, no gateway, no new dependency
(urllib only). Discord rules honored here: description <= 4096 chars,
colors green/amber/red, non-2xx never raises to the caller.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from .results import STATUS_FAILED, STATUS_SKIPPED

EMBED_DESC_LIMIT = 4096
COLOR_OK, COLOR_DRY, COLOR_BAD = 0x2ECC71, 0xF1C40F, 0xE74C3C
# Discord sits behind Cloudflare: the default Python-urllib UA gets HTTP 403
# error code 1010 (proven 2026-09-17), so we must announce ourselves.
USER_AGENT = "autoposter/0.1 (facebook group posting monitor)"


def build_payload(summary: dict, profile_name: str = "") -> dict:
    """summary = RunRecorder.summary(); pure -> trivially unit-testable."""
    dry = summary["dry_run"]
    aborted = summary.get("aborted")
    bad = bool(aborted) or summary["failed"] > 0
    color = COLOR_BAD if bad else (COLOR_DRY if dry else COLOR_OK)
    done = summary["published"] + summary["staged"]
    head = (f"**{'Staged (dry run)' if dry else 'Published'}: {done}/"
            f"{summary['attempted']}** · **Failed: {summary['failed']}** · "
            f"**Duration: {summary['duration_s']}s**")
    lines = [f"**⛔ Run aborted:** {aborted}" if aborted else head]
    totals = summary.get("totals_published_all_time", {})
    for g in summary["groups"]:
        name = str(g["name"])[:80]
        if g["status"] == STATUS_FAILED:
            lines.append(f"❌ {name} — `{str(g.get('error') or '')[:120]}`")
        elif g["status"] == STATUS_SKIPPED:
            lines.append(f"⏭️ {name} — no composer (not postable for us)")
        elif dry:
            lines.append(f"🧪 {name} — composer staged, NOT published")
        else:
            lines.append(f"✅ {name} — all-time published: "
                         f"{totals.get(str(g['group_id']), 0)}")
    title = (
        f"autoposter · {profile_name or 'autoposter'} · "
        f"{'DRY RUN' if dry else 'LIVE'} · {summary['run_id']}"
    )
    return {
        "username": "autoposter",
        "embeds": [{
            "title": title[:256],
            "description": "\n".join(lines)[:EMBED_DESC_LIMIT],
            "color": color,
            "timestamp": summary["started"],
            "footer": {"text": f"ledger: .local-capture results · run {summary['run_id']}"},
        }],
    }


def build_crosspost_payload(summary: dict, profile_name: str = "") -> dict:
    """summary = RunRecorder.summary_crosspost() — one embed per crosspost run
    (same never-fails philosophy as build_payload: pure function)."""
    dry = summary["dry_run"]
    aborted = summary.get("aborted")
    bad = bool(aborted) or summary["failed"] > 0
    color = COLOR_BAD if bad else (COLOR_DRY if dry else COLOR_OK)
    done = summary["published"] + summary["staged"]
    head = (f"**Batches {'staged (dry run)' if dry else 'published'}: {done}/"
            f"{summary['attempted']}** · **Failed: {summary['failed']}** · "
            f"**Duration: {summary['duration_s']}s**")
    lines = [f"**⛔ Run aborted:** {aborted}" if aborted else head]
    totals = summary.get("totals_crossposted_batches_all_time", {})
    for r in summary["listings"]:
        name = str(r["listing_title"])[:70]
        if r["status"] == STATUS_FAILED:
            lines.append(f"❌ {name} b{r['batch']} — `{str(r.get('error') or '')[:110]}`")
        elif r["status"] == STATUS_SKIPPED:
            lines.append(f"⏭️ {name} — {str(r.get('error') or '')[:110]}")
        elif dry:
            lines.append(f"🧪 {name} — batch {r['batch']}: {r['count']} groups "
                         f"checked, dialog CANCELLED (nothing published)")
        else:
            lines.append(f"✅ {name} — batch {r['batch']}: {r['count']} groups · "
                         f"all-time batches: {totals.get(str(r['listing_id']), 0)}")
    title = (f"autoposter crosspost · {profile_name or 'autoposter'} · "
             f"{'DRY RUN' if dry else 'LIVE'} · {summary['run_id']}")
    return {
        "username": "autoposter",
        "embeds": [{
            "title": title[:256],
            "description": "\n".join(lines)[:EMBED_DESC_LIMIT],
            "color": color,
            "timestamp": summary["started"],
            "footer": {"text": f"ledger: .local-capture results · run {summary['run_id']}"},
        }],
    }


def send_summary(webhook_url: str, payload: dict, log=print,
                 timeout: float = 10.0) -> bool:
    """POST the embed; one retry for transient errors. NEVER raises: a dead
    webhook must not change the poster run's exit code."""
    if not webhook_url:
        log("discord: AP_DISCORD_WEBHOOK_URL empty — skipping notification")
        return False
    data = json.dumps(payload).encode("utf-8")
    for attempt in (1, 2):
        req = urllib.request.Request(
            webhook_url, data=data, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status in (200, 204):
                    log(f"discord: summary sent (HTTP {resp.status})")
                    return True
                log(f"discord: unexpected HTTP {resp.status}")
        except urllib.error.HTTPError as e:
            # An HTTPError built with fp=None (e.g. constructed by http.client
            # before the body was read) has no readable body; the read must
            # never escape this handler (that would break "never raises").
            body = b""
            try:
                body = e.read()[:200]
            except Exception:  # noqa: BLE001 — body read is best-effort by design
                log(f"discord: no error body to read ({type(e).__name__})")
            log(f"discord: HTTP {e.code} {e.reason} — {body!r}")
            if e.code in (400, 401, 403, 404):   # bad webhook URL: no retry
                return False
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            log(f"discord: send error ({type(e).__name__}: {e})")
        if attempt == 1:
            time.sleep(2)
    return False


def _cli() -> int:
    """python -m poster.notify --test | --stats  (webhook/ledger setup smoke)."""
    import argparse

    from .config import load_config
    from .results import count_published

    ap = argparse.ArgumentParser(prog="poster.notify")
    ap.add_argument("--test", action="store_true",
                    help="send a fake dry-run summary to verify the webhook")
    ap.add_argument("--stats", action="store_true",
                    help="print all-time published count per group id")
    args = ap.parse_args()
    cfg = load_config()
    if args.stats:
        totals = count_published(cfg.ledger_file)
        if not totals:
            print("ledger empty (no live posts recorded yet)")
        for gid, n in sorted(totals.items(), key=lambda kv: -kv[1]):
            print(f"{gid}: {n} published")
        return 0
    if args.test:
        fake = {"run_id": "TEST", "dry_run": True, "aborted": None,
                "started": "2026-09-17T00:00:00+00:00", "duration_s": 1.5,
                "published": 0, "staged": 1, "failed": 0, "attempted": 1,
                "groups": [{"name": "webhook test", "group_id": "0",
                            "status": "staged", "error": None}],
                "totals_published_all_time": {}}
        payload = build_payload(fake, cfg.identity_label)
        return 0 if send_summary(cfg.discord_webhook_url, payload) else 1
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
