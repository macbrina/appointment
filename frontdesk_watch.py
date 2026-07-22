import asyncio
import hashlib
import json
import os
import random
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import List, Set

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser
from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
)


def load_dotenv(path: str | None = None) -> None:
    """Load environment variables from .env if they are not already set."""
    candidates = []
    if path:
        candidates.append(Path(path))
    candidates.extend(
        [
            Path(__file__).resolve().parent / ".env",
            Path.cwd() / ".env",
        ]
    )

    for candidate in candidates:
        if not candidate.exists():
            continue
        for raw_line in candidate.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)
        if os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_CHAT_ID"):
            break


load_dotenv()


def telegram_send(message: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "disable_web_page_preview": True,
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception:
        pass


def slots_fingerprint(slots: List[datetime]) -> str:
    """Stable fingerprint of the current availability list."""
    payload = "|".join(s.isoformat(timespec="minutes") for s in slots)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_telegram_message(slots: List[datetime], home_url: str) -> str:
    """Create a concise Telegram alert for current availability."""
    if not slots:
        return ""

    first_slot = slots[0]
    first_slot_text = first_slot.strftime("%a %d %b %Y at %H:%M")
    if len(slots) == 1:
        detail = f"First free slot: {first_slot_text}"
    else:
        detail = f"{len(slots)} free slots visible. First: {first_slot_text}"

    return "\n".join(
        [
            "Frontdesk booking alert",
            detail,
            f"Open: {home_url}",
        ]
    )


def build_heartbeat_message(home_url: str, interval_seconds: int = 24 * 60 * 60) -> str:
    """Create a concise Telegram heartbeat message used for startup and liveness."""
    if interval_seconds >= 24 * 60 * 60:
        interval_text = "24 hours"
    else:
        interval_text = f"{max(1, interval_seconds // 60)} minutes"

    return "\n".join(
        [
            "Frontdesk watcher heartbeat",
            "The watcher is still running and checking for new openings.",
            f"Next heartbeat in about {interval_text}.",
            f"Open: {home_url}",
        ]
    )


@dataclass
class Config:
    home_url: str = (
        "https://reservation.frontdesksuite.com/toender/vielse/Home/Index"
        "?pageid=8d47364a-5e21-4e40-892d-e9f46878e18b&culture=en&uiculture=en"
    )
    witness_choice: str = "no"

    interval_seconds: int = 20
    jitter_seconds: int = 5

    cutoff_year: int = 2099
    cutoff_month: int = 1
    cutoff_day: int = 1

    seen_file: str = "seen_slots.json"
    headless: bool = True

    telegram_min_interval_seconds: int = 30 * 60
    telegram_max_items: int = 10
    auto_click_first_slot: bool = False
    telegram_remind_if_still_available: bool = False
    last_fingerprint_file: str = "last_fingerprint.txt"
    heartbeat_file: str = "heartbeat.txt"
    heartbeat_interval_seconds: int = 24 * 60 * 60
    run_forever: bool = False


def cutoff_date(cfg: Config) -> date:
    return date(cfg.cutoff_year, cfg.cutoff_month, cfg.cutoff_day)


def load_seen(path: str) -> Set[str]:
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data) if isinstance(data, list) else set()
    except Exception:
        return set()


def save_seen(path: str, seen: Set[str]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, ensure_ascii=False, indent=2)


def load_last_fingerprint(path: str) -> str:
    try:
        if not os.path.exists(path):
            return ""
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def save_last_fingerprint(path: str, fp: str) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(fp or "")
    except Exception:
        pass


def load_last_heartbeat(path: str) -> float:
    try:
        if not os.path.exists(path):
            return 0.0
        with open(path, "r", encoding="utf-8") as f:
            return float(f.read().strip() or 0.0)
    except Exception:
        return 0.0


def save_last_heartbeat(path: str, ts: float) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(ts))
    except Exception:
        pass


def should_send_heartbeat(last_ts: float, now_ts: float, interval_seconds: int) -> bool:
    if last_ts <= 0:
        return True
    return (now_ts - last_ts) >= interval_seconds


def parse_times_from_html(html: str) -> List[datetime]:
    """
    Parse the time-selection page for available reservation slots.

    The site uses a day container such as div.date.one-queue and a day heading
    such as span.header-text. When a slot is available, the day body also contains
    a time element such as span.available-time.
    """
    soup = BeautifulSoup(html, "lxml")
    out: List[datetime] = []

    for day_div in soup.select("div.date.one-queue"):
        header = day_div.select_one("span.header-text")
        if not header:
            continue

        day_text = header.get_text(" ", strip=True)
        try:
            day = dtparser.parse(day_text, fuzzy=True).date()
        except Exception:
            continue

        time_candidates = day_div.select("span.available-time, a, button, span")
        for candidate in time_candidates:
            text = candidate.get_text(" ", strip=True)
            if not re.search(r"\b\d{1,2}:\d{2}\b", text):
                continue
            try:
                dt = dtparser.parse(f"{day.isoformat()} {text}", fuzzy=True)
                out.append(dt.replace(second=0, microsecond=0))
            except Exception:
                continue

    uniq = {x.isoformat(): x for x in out}
    return sorted(uniq.values())


def is_before_cutoff(dt: datetime, cfg: Config) -> bool:
    return dt.date() < cutoff_date(cfg)


async def accept_cookies_if_present(page) -> None:
    try:
        await page.get_by_role(
            "button", name=re.compile(r"accept necessary cookies", re.I)
        ).click(timeout=5000)
    except Exception:
        pass


async def select_witness_option(page, cfg: Config) -> None:
    choice_label = (
        "Yes - we will bring along our own witnesses"
        if cfg.witness_choice.lower() == "yes"
        else "No - we will not bring along our own witnesses"
    )
    try:
        await page.get_by_role(
            "link", name=re.compile(rf"^{re.escape(choice_label)}", re.I)
        ).first.click(timeout=15000)
    except Exception:
        pass


async def click_first_available_time(page) -> bool:
    try:
        locator = page.locator(
            "span.available-time, a.available-time, button.available-time"
        )
        if await locator.count() == 0:
            return False
        await locator.first.click(timeout=15000)
        return True
    except Exception:
        return False


async def get_available_slots(cfg: Config) -> List[datetime]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=cfg.headless)
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto(cfg.home_url, wait_until="domcontentloaded", timeout=30000)
        await accept_cookies_if_present(page)
        await select_witness_option(page, cfg)

        try:
            await page.wait_for_selector("div.date.one-queue", timeout=20000)
        except PlaywrightTimeoutError:
            html = await page.content()
            await context.close()
            await browser.close()
            return parse_times_from_html(html)

        html = await page.content()
        slots = parse_times_from_html(html)

        if cfg.auto_click_first_slot and slots:
            clicked = await click_first_available_time(page)
            if clicked:
                print("Clicked the first discovered available time slot.")
                print(f"Current page: {page.url}")

        await context.close()
        await browser.close()
        return slots


async def main_async() -> None:
    cfg = Config()
    cfg.witness_choice = (
        os.getenv("FRONTDESK_WITNESS_CHOICE", cfg.witness_choice).strip().lower()
        or cfg.witness_choice
    )
    cfg.auto_click_first_slot = (
        os.getenv("FRONTDESK_AUTO_CLICK_FIRST_SLOT", "").strip().lower()
        in {"1", "true", "yes"}
    ) or cfg.auto_click_first_slot
    cfg.heartbeat_interval_seconds = int(
        os.getenv(
            "FRONTDESK_HEARTBEAT_INTERVAL_SECONDS",
            str(cfg.heartbeat_interval_seconds),
        ).strip()
        or cfg.heartbeat_interval_seconds
    )
    cfg.run_forever = os.getenv("FRONTDESK_RUN_FOREVER", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }

    # Prefer a state directory if provided by the runtime/container.
    # Check ENV, then conventional mount path, then fallback to current working dir.
    state_dir = os.getenv("FRONTDESK_STATE_DIR", "")
    if not state_dir:
        # common mount point when using the named volume in docker-compose
        candidate = Path("/usr/src/app/state")
        if candidate.is_dir():
            state_dir = str(candidate)
        else:
            candidate = Path.cwd() / "state"
            if candidate.is_dir():
                state_dir = str(candidate)

    if state_dir:
        # ensure directory exists
        try:
            os.makedirs(state_dir, exist_ok=True)
        except Exception:
            pass
        cfg.seen_file = str(Path(state_dir) / Path(cfg.seen_file).name)
        cfg.last_fingerprint_file = str(
            Path(state_dir) / Path(cfg.last_fingerprint_file).name
        )
        cfg.heartbeat_file = str(Path(state_dir) / Path(cfg.heartbeat_file).name)

    seen = load_seen(cfg.seen_file)
    last_telegram_sent_at = 0.0
    # load persisted last fingerprint so we notify on changes across restarts
    last_fingerprint = load_last_fingerprint(cfg.last_fingerprint_file)
    last_heartbeat = load_last_heartbeat(cfg.heartbeat_file)
    now_ts = time.time()
    if should_send_heartbeat(last_heartbeat, now_ts, cfg.heartbeat_interval_seconds):
        telegram_send(
            build_heartbeat_message(cfg.home_url, cfg.heartbeat_interval_seconds)
        )
        last_heartbeat = now_ts
        save_last_heartbeat(cfg.heartbeat_file, last_heartbeat)

    print(f"Watching: {cfg.home_url}")
    print(f"Cutoff: before {cutoff_date(cfg).isoformat()}")
    if cfg.run_forever:
        print(
            f"Checking every {cfg.interval_seconds}s (+ up to {cfg.jitter_seconds}s jitter)."
        )
    else:
        print("Running a single scheduled check and exiting after completion.")

    iteration = 0
    while True:
        iteration += 1
        try:
            slots = await get_available_slots(cfg)
            good = [s for s in slots if is_before_cutoff(s, cfg)]
            after_cutoff = [s for s in slots if not is_before_cutoff(s, cfg)]

            now_str = datetime.now().isoformat(sep=" ", timespec="seconds")
            print(f"\n{now_str} — availability snapshot:")
            print(f"  before cutoff: {len(good)} slot(s)")
            for s in good:
                print("    ", s.isoformat(sep=" "))
            print(f"  on/after cutoff: {len(after_cutoff)} slot(s)")

            newly_found = []
            for s in good:
                k = s.isoformat()
                if k not in seen:
                    seen.add(k)
                    newly_found.append(s)

            if newly_found:
                print("  (new since last run)")
                save_seen(cfg.seen_file, seen)

            fp = slots_fingerprint(good)
            now_ts = time.time()

            should_notify_change = bool(good) and (fp != last_fingerprint)
            should_notify_reminder = (
                cfg.telegram_remind_if_still_available
                and bool(good)
                and (fp == last_fingerprint)
                and (
                    (now_ts - last_telegram_sent_at)
                    >= cfg.telegram_min_interval_seconds
                )
            )
            should_send_liveness = should_send_heartbeat(
                last_heartbeat,
                now_ts,
                cfg.heartbeat_interval_seconds,
            )

            if should_notify_change or should_notify_reminder:
                telegram_send(build_telegram_message(good, cfg.home_url))
                last_telegram_sent_at = now_ts
                last_fingerprint = fp
                # persist fingerprint so restarts still notice changes
                save_last_fingerprint(cfg.last_fingerprint_file, fp)

            if should_send_liveness:
                telegram_send(
                    build_heartbeat_message(
                        cfg.home_url, cfg.heartbeat_interval_seconds
                    )
                )
                last_heartbeat = now_ts
                save_last_heartbeat(cfg.heartbeat_file, last_heartbeat)
        except Exception as exc:
            print(
                f"{datetime.now().isoformat(sep=' ', timespec='seconds')} — error: {exc}"
            )

        if not cfg.run_forever:
            break

        await asyncio.sleep(
            cfg.interval_seconds + random.randint(0, cfg.jitter_seconds)
        )


def run() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    run()
