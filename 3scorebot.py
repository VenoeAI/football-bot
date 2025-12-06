#!/usr/bin/env python3
"""
3scorebot.py
A robust API-Football -> Telegram notifier:
- Sends a single nicely formatted startup message listing live matches
- Monitors live matches and sends a Telegram alert when a team reaches exactly 3 goals
- Uses API-Football (v3) "fixtures?live=all" endpoint
- Respects a configurable POLL_INTERVAL to avoid hitting request limits
- Test mode: load a local JSON file instead of calling the API (useful for dry-run)
"""

import requests
import time
import json
import logging
import signal
import sys
from datetime import datetime
import os

# ----------------------
# CONFIG (you already gave these; unchanged)
# ----------------------
API_KEY = os.getenv("API_FOOTBALL_KEY")  # API-Football.com
BASE_URL = "https://v3.football.api-sports.io/fixtures"
HEADERS = {"x-apisports-key": API_KEY}

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

POLL_INTERVAL = 865
TEST_MODE = True
TEST_FILE = "test_live.json"

LOG_FILE = "3scorebot.log"
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
console = logging.StreamHandler()
console.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
console.setFormatter(formatter)
logging.getLogger("").addHandler(console)

notified_matches = set()
startup_message_sent = False
running = True


# ----------------------
# Utility functions
# ----------------------
def send_telegram(message: str):
    """Send text message to Telegram."""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        params = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        logging.info("Telegram message sent.")
        return True
    except Exception as e:
        logging.error(f"Failed to send Telegram message: {e}")
        return False


def send_telegram_photo(photo_path, caption=""):
    """Send a photo with caption in ONE Telegram message."""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        with open(photo_path, "rb") as img:
            files = {"photo": img}
            data = {
                "chat_id": TELEGRAM_CHAT_ID,
                "caption": caption,
                "parse_mode": "Markdown"
            }
            r = requests.post(url, data=data, files=files, timeout=20)
            r.raise_for_status()
            logging.info("Telegram photo sent.")
            return True
    except Exception as e:
        logging.error(f"Failed to send Telegram photo: {e}")
        return False


def format_kickoff(iso_dt: str):
    try:
        dt = datetime.fromisoformat(iso_dt.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return iso_dt


def extract_goals(m: dict):
    gh = m.get("goals", {}).get("home")
    ga = m.get("goals", {}).get("away")

    if gh is None:
        gh = m.get("score", {}).get("halftime", {}).get("home", 0)
    if ga is None:
        ga = m.get("score", {}).get("halftime", {}).get("away", 0)

    try:
        gh = int(gh)
        ga = int(ga)
    except:
        gh, ga = 0, 0

    return gh, ga


def short_match_text(m: dict):
    home = m.get("teams", {}).get("home", {}).get("name", "Home")
    away = m.get("teams", {}).get("away", {}).get("name", "Away")
    gh, ga = extract_goals(m)
    status = m.get("fixture", {}).get("status", {}).get("short", "N/A")
    return f"{home} {gh}-{ga} {away} — {status}"


def format_startup_message(matches: list):
    if not matches:
        return "⚡ *Live Matches at Startup*\n\n_No live matches found at startup._"

    lines = ["⚡ *Live Matches at Startup*\n"]
    for m in matches:
        league = m.get("league", {}).get("name", "Unknown League")
        home = m.get("teams", {}).get("home", {}).get("name", "Home")
        away = m.get("teams", {}).get("away", {}).get("name", "Away")
        gh, ga = extract_goals(m)
        status = m.get("fixture", {}).get("status", {}).get("short", "N/A")
        kickoff = m.get("fixture", {}).get("date")
        kickoff_str = format_kickoff(kickoff) if kickoff else "Unknown time"

        status_emoji = {
            "1H": "🟢 In progress",
            "2H": "🟠 In progress (2H)",
            "HT": "⏸ Halftime",
            "FT": "🔵 Finished",
        }.get(status, status)

        lines.append(f"*{league}*")
        lines.append(f"{home} *{gh}* - *{ga}* {away}  —  {status_emoji}")
        lines.append(f"Kickoff: `{kickoff_str}`\n")

    return "\n".join(lines)


# ----------------------
# Fetching
# ----------------------
def fetch_live_matches():
    if TEST_MODE:
        logging.info("TEST_MODE enabled — loading test file: %s", TEST_FILE)
        try:
            with open(TEST_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "response" in data:
                return data.get("response", [])
            if isinstance(data, list):
                return data
            return []
        except Exception as e:
            logging.error("Failed to read test file: %s", e)
            return []

    params = {"live": "all"}
    try:
        r = requests.get(BASE_URL, headers=HEADERS, params=params, timeout=20)
        r.raise_for_status()
    except Exception as e:
        logging.error("HTTP error fetching matches: %s", e)
        return []

    try:
        data = r.json()
    except:
        logging.error("Bad JSON response")
        return []

    if "response" not in data:
        return []

    matches = data.get("response", [])
    logging.info("Fetched %d live matches", len(matches))
    return matches


# ----------------------
# Core logic
# ----------------------
def check_for_3goals_and_alert(matches: list):
    for m in matches:
        try:
            fixture = m.get("fixture", {})
            match_id = fixture.get("id")
            if match_id is None:
                continue

            gh, ga = extract_goals(m)
            home_name = m.get("teams", {}).get("home", {}).get("name", "Home")
            away_name = m.get("teams", {}).get("away", {}).get("name", "Away")
            status = fixture.get("status", {}).get("short", "")

            # NEW CRITERIA: Score must be EXACTLY 3–0 or 0–3
            if ((gh == 3 and ga == 0) or (gh == 0 and ga == 3)) and match_id not in notified_matches:

                scorer = home_name if gh == 3 else away_name

                caption = (
                    f"⚽ *GOAL ALERT!*\n\n"
                    f"{scorer} now leads *3 - 0*! \n\n"
                    f"{home_name} *{gh}* - *{ga}* {away_name}\n\n"
                    f"_Match status: {status}_\n\n"
                    f"🔥 *Stake Now!* 🔥\n"
                    f"_Avoid: Gibraltar, 2 Bundesliga, U-anything & England Championship, Arab Leagues_"
                )

                logging.info("Sending 3-0 alert: %s", short_match_text(m))

                time.sleep(2)

                send_telegram_photo("stake_now_small.jpg", caption)

                notified_matches.add(match_id)

        except Exception as e:
            logging.exception("Error evaluating match: %s", e)



# ----------------------
# Graceful shutdown handler
# ----------------------
def handle_exit(signum, frame):
    global running
    logging.info("Shutdown signal received. Exiting...")
    running = False


signal.signal(signal.SIGINT, handle_exit)
signal.signal(signal.SIGTERM, handle_exit)


# ----------------------
# Main loop
# ----------------------
def run_bot():
    global startup_message_sent, running

    logging.info("Bot starting. TEST_MODE=%s, poll interval=%s seconds", TEST_MODE, POLL_INTERVAL)

    while running:
        matches = fetch_live_matches()

        if not startup_message_sent:
            try:
                startup_msg = format_startup_message(matches)
                if TEST_MODE:
                    startup_msg = "⚠ _TEST MODE Enabled_\n\n" + startup_msg
                send_telegram(startup_msg)
                startup_message_sent = True
            except Exception as e:
                logging.exception("Startup message failed: %s", e)

        try:
            check_for_3goals_and_alert(matches)
        except Exception as e:
            logging.exception("Unexpected error in loop: %s", e)

        for _ in range(int(POLL_INTERVAL)):
            if not running:
                break
            time.sleep(1)

    logging.info("Bot stopped.")


# ----------------------
# Entry point
# ----------------------
if __name__ == "__main__":
    run_bot()

