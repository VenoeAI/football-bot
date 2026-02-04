#!/usr/bin/env python3
import time
import logging
import signal
import sys
import json
import os

# Telegram API calls use standard requests
import requests as standard_requests

# SofaScore API calls use curl_cffi for anti-bot bypass
try:
    from curl_cffi import requests
    from curl_cffi.requests import exceptions as cffi_exceptions
except ImportError:
    print("Error: Library 'curl_cffi' not found. Please run: pip install curl_cffi")
    sys.exit(1)


# --- CONFIGURATION ---
TELEGRAM_BOT_TOKEN = "8414736163:AAHk-RIqgTLiBC6M_fKGoKRBHDtxpoGvFEI"
TELEGRAM_CHAT_ID = "1584184290"

SOFASCORE_API_URL = "https://api.sofascore.com/api/v1/sport/football/events/live"
SOFASCORE_EVENT_INCIDENTS_URL = "https://api.sofascore.com/api/v1/event/{event_id}/incidents"

POLL_INTERVAL = 600  # 10 minutes
LOG_FILE = "3scorebot.log"

# --- Test Mode ---
TEST_MODE = False
TEST_FILE = "test_live.json"

# --- SCORELINE WATCHLIST ---
THREE_GOAL_SCORELINES = {(3, 0), (0, 3)}
# -----------------------------

# --- TEXT FILTER CONFIG ---
ALLOWED_TIME_KEYWORDS = ["1st half", "halftime"]

EXCLUDED_LEAGUES = [
    "Mexico - Liga TDP",
    "Kenya - Premier League",
    "Gibraltar - National League",
    "Finland - Liigacup, Group A",
    "Iraq - Iraq Stars League",
    "Greece - Stoiximan Super League",
]
# -----------------------------

# --- Logging Setup ---
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
console = logging.StreamHandler()
console.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
console.setFormatter(formatter)
logging.getLogger("").addHandler(console)

notified_matches = set()
startup_message_sent = False
running = True

# --- Headers ---
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Referer": "https://www.sofascore.com/",
    "Origin": "https://www.sofascore.com"
}


def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        params = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }
        r = standard_requests.post(url, data=params, timeout=20)
        r.raise_for_status()
        return True
    except Exception as e:
        logging.error("Telegram error: %s", e)
        return False


def send_telegram_photo(photo_path, caption=""):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        with open(photo_path, "rb") as img:
            files = {"photo": img}
            data = {
                "chat_id": TELEGRAM_CHAT_ID,
                "caption": caption,
                "parse_mode": "Markdown"
            }
            r = standard_requests.post(url, data=data, files=files, timeout=20)
            r.raise_for_status()
        return True
    except FileNotFoundError:
        logging.warning("Photo not found, sending text only.")
        return send_telegram(caption)
    except Exception as e:
        logging.error("Photo send error: %s", e)
        return False


def parse_sofascore_match(match):
    home = match.get("homeTeam", {}).get("name", "Home")
    away = match.get("awayTeam", {}).get("name", "Away")

    gh = match.get("homeScore", {}).get("current", 0) or 0
    ga = match.get("awayScore", {}).get("current", 0) or 0

    status = match.get("status", {}).get("description", "In Play")
    league = match.get("tournament", {}).get("name", "Unknown League")
    category = match.get("tournament", {}).get("category", {}).get("name", "")
    full_league = f"{category} - {league}" if category else league

    return {
        "id": match.get("id"),
        "home": home,
        "away": away,
        "gh": int(gh),
        "ga": int(ga),
        "status": status,
        "league": full_league
    }


def fetch_live_matches():
    if TEST_MODE:
        if not os.path.exists(TEST_FILE):
            logging.error("Test file not found.")
            return []
        with open(TEST_FILE, "r") as f:
            return json.load(f).get("events", [])

    try:
        response = requests.get(
            SOFASCORE_API_URL,
            headers=HEADERS,
            impersonate="chrome120",
            timeout=20
        )
        response.raise_for_status()
        return response.json().get("events", [])
    except cffi_exceptions.RequestException as e:
        logging.error(f"SofaScore request error: {e}")
        return []
    except Exception as e:
        logging.error(f"Unexpected fetch error: {e}")
        return []


def fetch_goal_times(event_id, leading_is_home=True):
    """
    Fetch goal times for the leading team from SofaScore incidents API.
    Returns a sorted list of minute integers.
    """
    url = SOFASCORE_EVENT_INCIDENTS_URL.format(event_id=event_id)
    try:
        r = requests.get(url, headers=HEADERS, impersonate="chrome120", timeout=20)
        r.raise_for_status()
        data = r.json()

        incidents = data.get("incidents", [])
        goal_times = []

        for inc in incidents:
            if inc.get("type") != "goal":
                continue

            is_home = inc.get("isHome", None)
            if is_home is None:
                continue

            if leading_is_home and not is_home:
                continue
            if not leading_is_home and is_home:
                continue

            minute = inc.get("time")
            if isinstance(minute, int):
                goal_times.append(minute)

        goal_times.sort()
        return goal_times

    except Exception as e:
        logging.error(f"Failed to fetch goal times for event {event_id}: {e}")
        return []


def match_passes_text_filters(match_dict):
    """
    Build a plain text line and decide purely by text parsing.
    """
    text_line = f"{match_dict['status']} | {match_dict['league']} | {match_dict['home']} {match_dict['gh']}-{match_dict['ga']} {match_dict['away']}"
    text_lower = text_line.lower()

    # Block women's leagues
    if "women" in text_lower:
        return False

    # Check allowed times
    if not any(k in text_lower for k in ALLOWED_TIME_KEYWORDS):
        return False

    # Check excluded leagues
    for bad in EXCLUDED_LEAGUES:
        if bad.lower() in text_lower:
            return False

    return True


def format_startup_message(matches):
    filtered = []
    for m in matches:
        d = parse_sofascore_match(m)
        if match_passes_text_filters(d):
            filtered.append(d)

    top = filtered[:10]
    if not top:
        return "⚡ *Live Matches at Startup*\n\n_No matching live matches found._"

    lines = ["⚡ *Top 10 Live Matches at Startup*\n"]
    for d in top:
        lines.append(f"*{d['league']}*")
        lines.append(f"{d['home']} *{d['gh']}* - *{d['ga']}* {d['away']} — {d['status']}\n")

    lines.append(f"_{len(filtered)} matching live matches found._")
    return "\n".join(lines)


def check_for_score_alerts(matches):
    for m in matches:
        d = parse_sofascore_match(m)

        # Apply text-based filters
        if not match_passes_text_filters(d):
            continue

        match_id = d["id"]
        if not match_id or match_id in notified_matches:
            continue

        gh, ga = d["gh"], d["ga"]
        score = (gh, ga)

        # --- 3–0 / 0–3 ---
        if score in THREE_GOAL_SCORELINES:
            leading_is_home = gh > ga
            leader = d["home"] if leading_is_home else d["away"]

            # Fetch goal times for leading team
            goal_times = fetch_goal_times(match_id, leading_is_home=leading_is_home)

            # We need at least 3 goals to evaluate timing
            if len(goal_times) >= 3:
                first_goal = goal_times[0]
                third_goal = goal_times[2]
                diff = third_goal - first_goal

                # If goals came too fast, skip alert
                if diff < 10:
                    logging.info(
                        f"Skipping alert for {leader} ({d['home']} vs {d['away']}), goals too fast: {first_goal}' to {third_goal}' ({diff} min)"
                    )
                    continue

            # Build caption with copy-friendly team name
            caption = (
                f"⚽ *GOAL ALERT!*\n\n"
                f"`{leader}` leads *{gh} - {ga}*\n\n"
                f"{d['home']} *{gh}* - *{ga}* {d['away']}\n"
                f"⏱️ **{d['status']}**\n"
                f"🏆 {d['league']}\n\n"
                f"🔥 *Stake Now!* 🔥"
            )
            if send_telegram_photo("stake_now_small.jpg", caption):
                notified_matches.add(match_id)


def handle_exit(signum, frame):
    global running
    logging.info("Shutting down bot...")
    running = False


signal.signal(signal.SIGINT, handle_exit)
signal.signal(signal.SIGTERM, handle_exit)


def run_bot():
    global startup_message_sent

    logging.info("Bot started.")
    while running:
        matches = fetch_live_matches()

        check_for_score_alerts(matches)

        if not startup_message_sent:
            send_telegram(format_startup_message(matches))
            for m in matches:
                d = parse_sofascore_match(m)
                if match_passes_text_filters(d) and (d["gh"], d["ga"]) in THREE_GOAL_SCORELINES:
                    notified_matches.add(d["id"])
            startup_message_sent = True

        logging.info(f"Sleeping for {POLL_INTERVAL} seconds...")
        for _ in range(POLL_INTERVAL):
            if not running:
                break
            time.sleep(1)


if __name__ == "__main__":
    run_bot()
