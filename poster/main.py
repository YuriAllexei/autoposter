"""Runner: config + browser + per-group loop. Entry: python -m poster.main

Flow per run:
  1. load .env config (fail-safe dry-run), groups.json, post.txt verbatim,
     ordered car photos
  2. launch persistent Firefox (login lives in the profile; manual login is
     watched-for, credentials are NEVER typed by the bot)
  3. ensure active identity = professional posting profile (proven switcher)
  4. per enabled group (cap AP_MAX_POSTS_PER_RUN): human delay -> goto group
     -> wait for composer trigger -> dispatch flow by posting_code ->
     (live) verify via my_pending_content
  5. a failed group is logged + evidence-saved; other groups continue
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from playwright.async_api import async_playwright

from .config import Config, load_config
from .fb import dump_evidence, ensure_active_profile, ensure_login
from .flows import FlowError, Post, get_flow, human_sleep
from .photos import collect_photos


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


def load_groups(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    groups = data.get("groups", data if isinstance(data, list) else [])
    return [g for g in groups if g.get("enabled", True)]


def build_post(cfg: Config) -> Post:
    text = cfg.post_text_file.read_text(encoding="utf-8")  # 1:1, verbatim
    cars = collect_photos(cfg.photos_dir, cfg.photo_extensions, cfg.max_photos_per_post)
    total = sum(len(c.files) for c in cars)
    print(f"[run] post.txt: {len(text)} chars | photos: {total} in {len(cars)} car(s): "
          + ", ".join(f"{c.name}({len(c.files)})" for c in cars))
    return Post(text=text, cars=cars)


async def wait_group_ready(page, timeout_s: int = 45) -> bool:
    """Feed hydration: composer trigger text visible, scrolled to top."""
    from .flows import COMPOSER_TRIGGER_RE

    try:
        await page.get_by_text(COMPOSER_TRIGGER_RE).first.wait_for(
            state="visible", timeout=timeout_s * 1000
        )
        return True
    except Exception:
        return False


async def verify_pending(page, group_url: str, cfg: Config, log) -> None:
    """[proven path] group posts go through admin approval -> check
    /groups/<id>/my_pending_content/ and save evidence screenshot."""
    pending = group_url.rstrip("/") + "/my_pending_content/"
    await human_sleep(cfg, log, "pending-content check")
    try:
        await page.goto(pending, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(8000)
        shot = await dump_evidence(page, cfg.screenshot_dir, "pending_content")
        log(f"verify: pending-content page captured -> {shot}")
    except Exception as e:
        log(f"verify: could not open {pending}: {type(e).__name__}")


async def run_group(page, group: dict, post: Post, cfg: Config, log) -> bool:
    code = group["posting_code"]
    flow = get_flow(code)  # unknown code = KeyError, hard stop (never guess)
    await human_sleep(cfg, log, f"open group {group['name']!r}")
    log(f"[group] {group['name']} -> {group['group_url']} (flow {code})")
    try:
        await page.goto(group["group_url"], wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        log(f"[group] goto note: {type(e).__name__}")
    if not await wait_group_ready(page):
        shot = await dump_evidence(page, cfg.screenshot_dir, "feed_not_hydrated",
                                   html=await page.content())
        log(f"[group] FAIL composer never hydrated. Evidence: {shot}")
        return False
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(2000)
    try:
        await flow(page, post, cfg, log)
    except FlowError as e:
        log(f"[group] FLOW ERROR: {e}")
        try:
            await page.keyboard.press("Escape")  # close composer, leave nothing staged
        except Exception:
            pass
        return False
    if not cfg.dry_run:
        await verify_pending(page, group["group_url"], cfg, log)
    return True


async def main_async(cfg: Config, only_group: str | None) -> int:
    groups = load_groups(cfg.groups_file)
    if only_group:
        groups = [g for g in groups if only_group in g["group_url"] or only_group == g["name"]]
    if not groups:
        print("[run] no enabled groups matched — nothing to do")
        return 1
    if not cfg.post_text_file.exists():
        print(f"[run] missing {cfg.post_text_file} — nothing to post")
        return 1
    post = build_post(cfg)

    mode = "DRY RUN (will NOT publish)" if cfg.dry_run else "*** LIVE — WILL PUBLISH ***"
    print(f"[run] mode: {mode} | groups this run: {min(len(groups), cfg.max_posts_per_run)}"
          f" | profile: {cfg.profile_dir}")

    cfg.profile_dir.mkdir(parents=True, exist_ok=True)
    cfg.screenshot_dir.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        ctx = await p.firefox.launch_persistent_context(
            str(cfg.profile_dir), headless=cfg.headless,
            viewport={"width": cfg.viewport_width, "height": cfg.viewport_height},
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        rc = 1
        try:
            def log(msg: str) -> None:
                print(f"[{datetime.now(UTC).strftime('%H:%M:%S')}] {msg}", flush=True)

            if not await ensure_login(ctx, page, log):
                return 2
            if not await ensure_active_profile(
                ctx, page, posting_user_id=cfg.fb_posting_user,
                posting_name=cfg.fb_posting_profile_name, log=log,
            ):
                log("abort: not posting as the professional profile")
                return 3

            ok = 0
            for group in groups[: cfg.max_posts_per_run]:
                if await run_group(page, group, post, cfg, log):
                    ok += 1
                await human_sleep(cfg, log, "next group")
            log(f"done: {ok} group(s) processed of "
                f"{min(len(groups), cfg.max_posts_per_run)} queued "
                f"({'dry-run staged' if cfg.dry_run else 'published'})")
            rc = 0 if ok else 1
        finally:
            await ctx.close()  # flush profile cookies
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="poster", description="Facebook group auto-poster (see AGENTS.md)")
    ap.add_argument("--group", help="only this group (url substring or exact name)")
    ap.add_argument("--dry-run", dest="dry", action="store_true", default=None,
                    help="force dry run regardless of .env")
    ap.add_argument("--live", dest="live", action="store_true",
                    help="publish for real (overrides .env AP_DRY_RUN)")
    ap.add_argument("--headless", action="store_true", help="override AP_HEADLESS")
    args = ap.parse_args()

    cfg = load_config()
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
