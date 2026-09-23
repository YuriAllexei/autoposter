"""Runner: config + browser + per-group loop. Entry: python -m poster.main

Flow per run (dynamic-targets model, 2026-09-22 — data/groups.json is DEAD):
  1. load .env config (fail-safe dry-run), post.txt verbatim, ordered photos
  2. launch persistent Firefox (login lives in the profile; manual login is
     watched-for, credentials are NEVER typed by the bot)
  3. ensure active identity = configured poster (profile or page; [proven
     recording 20260922T210535Z])
  4. FETCH the identity's joined groups live [proven recording
     20260922T214219Z] and rotate: groups never attempted first (FB joins
     order), then oldest last-attempt from the ledger; cap
     AP_MAX_POSTS_PER_RUN so ~31 groups are covered over several runs
  5. per target: human delay -> goto group -> 5s composer gate ("Escribe
     algo..." rendered?) -> if not, skip (posting not enabled for our
     identity there); else run group_composer_es_v1 -> (live) verify via
     my_pending_content
  6. a failed/skipped group is logged (+evidence on fail); others continue
"""
from __future__ import annotations

import argparse
import asyncio
import random
import re
import sys
from datetime import UTC, datetime

from playwright.async_api import async_playwright

from .config import Config, load_config
from .fb import adopt_identity, dump_evidence, launch, make_log
from .flows import COMPOSER_TRIGGER_RE, FlowError, Post, group_composer_es_v1, human_sleep
from .groups_fetch import GroupsFetchError, fetch_joined_groups
from .notify import build_payload, send_summary
from .photos import collect_photos
from .results import RunRecorder, last_attempt_ts, select_targets

# runner-internal outcome of ONE group visit (ledger STATUS_* mapping
# happens in the loop: posted -> staged/published by dry_run flag)
POSTED, SKIPPED, FAILED = "posted", "skipped", "failed"


class Tee:
    def __init__(self, *sinks):
        self.sinks = sinks

    def write(self, s):
        for sink in self.sinks:
            sink.write(s)
        return len(s)

    def flush(self):
        for sink in self.sinks:
            sink.flush()


def build_post(cfg: Config) -> Post:
    text = cfg.post_text_file.read_text(encoding="utf-8")  # 1:1, verbatim
    cars = collect_photos(cfg.photos_dir, cfg.photo_extensions, cfg.max_photos_per_post)
    total = sum(len(c.files) for c in cars)
    print(f"[run] post.txt: {len(text)} chars | photos: {total} in {len(cars)} car(s): "
          + ", ".join(f"{c.name}({len(c.files)})" for c in cars))
    return Post(text=text, cars=cars)


# Buy/sell groups can open on their MARKETPLACE tab, where the composer is
# "Vender algo" (a sell form, NOT our proven text-post layout). Evidence:
# shots/no_composer_20260922T235159Z.png (group 249803862915566).
MARKETPLACE_TRIGGER_RE = re.compile(r"^\s*(?:Vender algo|Sell something)",
                                    re.IGNORECASE)
GATE_TAB_CLICK_JS = r"""
(() => {
  const norm = t => (t || "").replace(/\s+/g, " ").trim();
  const tab = Array.from(document.querySelectorAll('[role="tab"]')).find(
    el => /^(Publicaciones|Publicaci[oó]n|Posts|Discussion)$/i.test(norm(el.innerText)));
  if (!tab) return "no-tab";
  tab.click();
  return "clicked";
})()
"""


async def composer_gate(page, timeout_s: float = 5.0) -> tuple[bool, str]:
    """USER RULE (2026-09-22): the SOLE posting-capability gate — does the
    'Escribe algo...' trigger RENDER within 5s of the group page loading?
    Not present => skip. One bounded recovery: buy/sell groups sometimes land
    on their marketplace tab, so click Publicaciones/Posts once and re-check
    (tab click is navigation only; posting stays gated)."""
    try:
        await page.get_by_text(COMPOSER_TRIGGER_RE).first.wait_for(
            state="visible", timeout=timeout_s * 1000
        )
        return True, ""
    except Exception:
        pass
    try:
        if await page.evaluate(GATE_TAB_CLICK_JS) == "clicked":
            await page.get_by_text(COMPOSER_TRIGGER_RE).first.wait_for(
                state="visible", timeout=timeout_s * 1000)
            return True, ""
    except Exception:
        pass
    try:
        if await page.get_by_text(MARKETPLACE_TRIGGER_RE).first.is_visible():
            return False, " (marketplace tab 'Vender algo' — unproven layout)"
    except Exception:
        pass
    return False, ""


async def verify_pending(page, group_url: str, cfg: Config, log) -> None:
    """[proven path] group posts go through admin approval -> check
    /groups/<id>/my_pending_content/ and save evidence screenshot."""
    pending = group_url.rstrip("/") + "/my_pending_content/"
    await human_sleep(cfg, log, "pending-content check")
    try:
        await page.goto(pending, wait_until="domcontentloaded", timeout=60000)
        # pending-list render settle (page wait, not an anti-detection
        # sleep — deliberately outside the human_sleep cfg knobs)
        await page.wait_for_timeout(random.uniform(2000, 4000))
        shot = await dump_evidence(page, cfg.screenshot_dir, "pending_content")
        log(f"verify: pending-content page captured -> {shot}")
    except Exception as e:
        log(f"verify: could not open {pending}: {type(e).__name__}")


async def run_group(
    page, group: dict, post: Post, cfg: Config, log
) -> tuple[str, str | None]:
    """One target group. Returns ('posted'|'skipped'|'failed', error).
    group = a live-joined record {id, name, url} from poster.groups_fetch."""
    url = group["url"]
    log(f"[group] {group['name']} -> {url}")
    goto_err: str | None = None
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        # tolerated: domcontentloaded can stall while the SPA still renders
        goto_err = f"{type(e).__name__}: {e}"
        log(f"[group] goto note: {goto_err}")
    gate_ok, gate_why = await composer_gate(page)
    if not gate_ok:
        if goto_err:
            # AGENTS rule 4: "skipped" MEANS composer-not-enabled. A dead
            # navigation is an ERROR — never record it as not-postable.
            log("[group] FAIL goto broke AND no composer rendered")
            return FAILED, f"goto failed ({goto_err}); composer never seen"
        log(f"[group] skip: composer trigger never rendered (5s gate){gate_why}")
        try:
            shot = await dump_evidence(page, cfg.screenshot_dir, "no_composer",
                                       html=await page.content())
            log(f"[group] skip evidence: {shot}")
        except Exception:
            pass
        return SKIPPED, f"no composer within 5s{gate_why}"
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(2000)
    try:
        await group_composer_es_v1(page, post, cfg, log)
    except FlowError as e:
        log(f"[group] FLOW ERROR: {e}")
        try:
            await page.keyboard.press("Escape")  # close composer, leave nothing staged
        except Exception:
            pass
        return FAILED, f"flow_error: {e}"
    if not cfg.dry_run:
        await verify_pending(page, url, cfg, log)
    return POSTED, None


def make_finish(cfg: Config, recorder: RunRecorder):
    """THE one Discord message of the run — call exactly once, at the end.

    Returns a closure finish(aborted=None): never raises, never sends twice
    (monitoring must not change the poster run's exit code).
    """
    sent = False

    def finish(aborted: str | None = None, log=print) -> None:
        nonlocal sent
        if sent or not cfg.discord_webhook_url:
            if not sent:
                log("monitor: AP_DISCORD_WEBHOOK_URL not set — no Discord summary")
            return
        sent = True
        try:
            summary = recorder.summary(aborted=aborted)
            send_summary(
                cfg.discord_webhook_url,
                build_payload(summary, cfg.identity_label), log)
        except Exception as e:  # monitoring is best-effort by design
            log(f"monitor: {type(e).__name__}: {e}")

    return finish


async def main_async(cfg: Config, only_group: str | None) -> int:
    recorder = RunRecorder(
        ledger_path=cfg.ledger_file,
        run_id=datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
        dry_run=cfg.dry_run)
    finish = make_finish(cfg, recorder)
    try:
        post = build_post(cfg)
    except FileNotFoundError:
        print(f"[run] missing {cfg.post_text_file} — nothing to post")
        finish("run aborted: missing data/post.txt")
        return 1

    mode = "DRY RUN (will NOT publish)" if cfg.dry_run else "*** LIVE — WILL PUBLISH ***"
    ident_label = cfg.identity_label
    print(f"[run] mode: {mode} | as: {cfg.post_as}={ident_label or '(unset)'}"
          f" | profile: {cfg.profile_dir}")

    cfg.screenshot_dir.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        ctx, page = await launch(cfg, p)
        rc = 1
        log = make_log()

        try:
            state = await adopt_identity(ctx, page, cfg, log)
            if state == "login":
                finish("run aborted: not logged in (c_user never appeared)", log)
                return 2
            if state == "identity":
                log("abort: not posting as the configured identity")
                finish(f"run aborted: not posting as "
                       f"{ident_label or cfg.post_as} (identity switch failed)", log)
                return 3

            # ---- dynamic targets: live joined groups of THIS identity ----
            try:
                joined = await fetch_joined_groups(page, av=cfg.identity_id,
                                                   log=log)
            except GroupsFetchError as e:
                log(f"abort: joined-groups fetch failed: {e}")
                finish(f"run aborted: joined-groups fetch failed: {e}", log)
                return 4
            last = last_attempt_ts(cfg.ledger_file)
            planned = select_targets(joined, last, cfg.max_posts_per_run,
                                     only=only_group)
            # full self-audit view: every joined group, its rotation clock
            # and which ones THIS run will attempt ('*')
            log(f"joined {len(joined)} group(s) as {cfg.identity_label!r} "
                f"(* = this run):")
            chosen = {g["id"] for g in planned}
            for g in joined:
                when = (last.get(g["id"]) or "never")[:16].replace("T", " ")
                star = "*" if g["id"] in chosen else " "
                log(f"  {star} {g['id']:<18} {when:<16} {g['name'][:60]}")
            if not planned:
                finish("run ended: no target groups selected"
                       + (f" (filter {only_group!r} matched nothing)"
                          if only_group else ""), log)
                return 0 if not only_group else 1

            ok = 0
            skipped = 0
            # anti-detection pause after login/profile-switch/joins-fetch,
            # before the 1st group
            await human_sleep(cfg, log, "first group")
            for idx, group in enumerate(planned):
                status, err = await run_group(page, group, post, cfg, log)
                if status == POSTED:
                    recorder.record(group, True)
                    ok += 1
                elif status == SKIPPED:
                    recorder.record_skipped(group, err or "no composer")
                    skipped += 1
                elif status == FAILED:
                    recorder.record(group, False, err)
                else:  # unknown status = programmer error, say so LOUD
                    raise RuntimeError(f"run_group returned unknown status {status!r}")
                if idx + 1 < len(planned):
                    # USER RULE (2026-09-22): group change = uniform
                    # 10-15s after finishing one group, before opening the
                    # next. General action sleeps stay 2-4s.
                    await human_sleep(cfg, log, "change to next group",
                                      cfg.group_switch_min, cfg.group_switch_max)
            log(f"done: {ok}/{len(planned)} "
                f"{'staged (dry-run)' if cfg.dry_run else 'published'}"
                + (f", {skipped} skipped (no composer)" if skipped else ""))
            rc = 0 if ok else 1
            finish(None, log)
        except Exception as e:
            # keep the hard-error traceback, but let Discord know the
            # run died before re-raising.
            log(f"[run] FATAL: {type(e).__name__}: {e}")
            finish(f"run crashed: {type(e).__name__}: {e}", log)
            raise
        finally:
            await ctx.close()  # flush profile cookies
    return rc


async def list_groups_async(cfg: Config) -> int:
    """--list-groups: log in, adopt the configured identity, and fetch the
    ACTUAL joined-groups list via the proven GroupsCometJoinsRootQuery
    [recording 20260922T214219Z]. Read-only: opens the account's own list,
    never posts. Annotates each group with its rotation state (ledger)."""

    async with async_playwright() as p:
        ctx, page = await launch(cfg, p)
        log = make_log()
        try:
            state = await adopt_identity(ctx, page, cfg, log)
            if state == "login":
                log("abort: not logged in")
                return 2
            if state == "identity":
                log("abort: identity not active")
                return 3
            try:
                groups = await fetch_joined_groups(page, av=cfg.identity_id,
                                                   log=log)
            except GroupsFetchError as e:
                log(f"joins fetch failed: {e}")
                return 4
            last = last_attempt_ts(cfg.ledger_file)
            print(f"\nJOINED GROUPS for identity {cfg.identity_label!r} "
                  f"({cfg.identity_id}) — {len(groups)} total\n")
            print(f"  {'group id':<18} last-attempt   name")
            print("  " + "-" * 92)
            for g in groups:
                when = (last.get(g["id"]) or "never")[:16].replace("T", " ")
                print(f"  {g['id']:<18} {when:<14} {g['name'][:64]}")
            out = (cfg.screenshot_dir.parent / "groups" /
                   f"joined_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json")
            out.parent.mkdir(parents=True, exist_ok=True)
            import json as _json
            out.write_text(_json.dumps(groups, indent=2, ensure_ascii=False),
                           encoding="utf-8")
            print(f"\n[saved] {out}")
            return 0
        finally:
            await ctx.close()


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="poster", description="Facebook group auto-poster (see AGENTS.md)")
    ap.add_argument("--group", help="only this group (url substring or exact name)")
    ap.add_argument("--list-groups", action="store_true",
                    help="open the browser, become the configured identity, "
                         "and FETCH the account's joined groups live "
                         "(read-only; [recording 20260922T214219Z])")
    ap.add_argument("--dry-run", dest="dry", action="store_true", default=None,
                    help="force dry run regardless of .env")
    ap.add_argument("--live", dest="live", action="store_true",
                    help="publish for real (overrides .env AP_DRY_RUN)")
    ap.add_argument("--headless", action="store_true", help="override AP_HEADLESS")
    args = ap.parse_args()

    cfg = load_config()
    if args.list_groups:
        return asyncio.run(list_groups_async(cfg))
    if args.dry:
        cfg.dry_run = True
    if args.live and not args.dry:
        # --live only works if .env already says AP_DRY_RUN=false (repo rule:
        # going live is an explicit file edit, never just a CLI flag)
        if cfg.dry_run:
            print("[run] --live requested but AP_DRY_RUN is true in .env — "
                  "refusing (edit .env explicitly to go live)")
            return 4
        cfg.dry_run = False
    if args.headless:
        cfg.headless = True

    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    logfile = cfg.log_dir / f"run_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.log"
    sys.stdout = Tee(sys.__stdout__, open(logfile, "w", encoding="utf-8"))
    try:
        return asyncio.run(main_async(cfg, args.group))
    finally:
        sys.stdout.flush()
        sys.stdout = sys.__stdout__
        print(f"[run] log saved: {logfile}")


if __name__ == "__main__":
    sys.exit(main())
