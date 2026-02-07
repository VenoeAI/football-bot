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

POLL_INTERVAL = 600  # 10 minutes
LOG_FILE = "3scorebot.log"

# --- Test Mode ---
TEST_MODE = False
TEST_FILE = "test_live.json"

# --- SCORELINE WATCHLIST ---
THREE_GOAL_SCORELINES = {(3, 0), (0, 3)}
DRAW_SCORELINES = {(2, 2), (3, 3), (4, 4)}
# -----------------------------

# --- TEXT FILTER CONFIG ---
ALLOWED_TIME_KEYWORDS = ["1st half", "halftime", "HT", "Live"]

EXCLUDED_LEAGUES = [
    "Mexico - Liga TDP",
    "Kenya - Premier League",
    "Gibraltar - National League",
    "Finland - Liigacup, Group A",
    "Iraq - Iraq Stars League",
    "Greece - Stoiximan Super League",
]

PENALTY_KEYWORDS = ["penalty", "pen."]  # text-based scan
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


def match_has_penalty_text(match):
    """
    Convert entire match JSON to text and look for penalty keywords.
    Pure text parsing, no structured field logic.
    """
    try:
        blob = json.dumps(match).lower()
    except Exception:
        blob = str(match).lower()

    for k in PENALTY_KEYWORDS:
        if k in blob:
            return True
    return False


def match_passes_text_filters(match_dict, raw_match):
    """
    Build a plain text line and decide purely by text parsing.
    """
    text_line = f"{match_dict['status']} | {match_dict['league']} | {match_dict['home']} {match_dict['gh']}-{match_dict['ga']} {match_dict['away']}"
    text_lower = text_line.lower()

    # Check allowed times
    if not any(k in text_lower for k in ALLOWED_TIME_KEYWORDS):
        return False

    # Check excluded leagues
    for bad in EXCLUDED_LEAGUES:
        if bad.lower() in text_lower:
            return False

    # Check for penalties anywhere in match text
    if match_has_penalty_text(raw_match):
        return False

    return True


def format_startup_message(matches):
    filtered = []
    for m in matches:
        d = parse_sofascore_match(m)
        if match_passes_text_filters(d, m):
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

        # Apply text-based filters (time, league, penalty)
        if not match_passes_text_filters(d, m):
            continue

        match_id = d["id"]
        if not match_id or match_id in notified_matches:
            continue

        gh, ga = d["gh"], d["ga"]
        score = (gh, ga)

        # --- 3–0 / 0–3 ---
        if score in THREE_GOAL_SCORELINES:
            leader = d["home"] if gh > ga else d["away"]
            caption = (
                f"⚽ *GOAL ALERT!*\n\n"
                f"{leader} leads *{gh} - {ga}*\n\n"
                f"{d['home']} *{gh}* - *{ga}* {d['away']}\n"
                f"⏱️ **{d['status']}**\n"
                f"🏆 {d['league']}\n\n"
                f"🔥 *Stake Now!* 🔥"
            )
            if send_telegram_photo("stake_now_small.jpg", caption):
                notified_matches.add(match_id)

        # --- 2–2 / 3–3 / 4–4 ---
        elif score in DRAW_SCORELINES:
            caption = (
                f"⚠️ *HIGH-SCORING DRAW!*\n\n"
                f"{d['home']} *{gh}* - *{ga}* {d['away']}\n"
                f"⏱️ **{d['status']}**\n"
                f"🏆 {d['league']}\n\n"
                f"🔥 *Momentum High — Expect Late Action!* 🔥"
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
                if match_passes_text_filters(d, m) and (d["gh"], d["ga"]) in (THREE_GOAL_SCORELINES | DRAW_SCORELINES):
                    notified_matches.add(d["id"])
            startup_message_sent = True

        logging.info(f"Sleeping for {POLL_INTERVAL} seconds...")
        for _ in range(POLL_INTERVAL):
            if not running:
                break
            time.sleep(1)


if __name__ == "__main__":
    run_bot()

