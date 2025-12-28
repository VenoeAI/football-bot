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
    # --- NEW: Explicitly import the exceptions module from curl_cffi ---
    from curl_cffi.requests import exceptions as cffi_exceptions
except ImportError:
    print("Error: Library 'curl_cffi' not found. Please run: pip install curl_cffi")
    sys.exit(1)


# --- CONFIGURATION ---
TELEGRAM_BOT_TOKEN = "8414736163:AAHk-RIqgTLiBC6M_fKGoKRBHDtxpoGvFEI"
TELEGRAM_CHAT_ID = "1584184290"

SOFASCORE_API_URL = "https://api.sofascore.com/api/v1/sport/football/events/live"

POLL_INTERVAL = 600  # Updated to 10 minutes (600 seconds)
LOG_FILE = "3scorebot.log"

# --- Test Mode Configuration ---
TEST_MODE = False
TEST_FILE = "test_live.json"
# -----------------------------

# --- Logging Setup ---
logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
console = logging.StreamHandler()
console.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
console.setFormatter(formatter)
logging.getLogger("").addHandler(console)

notified_matches = set()
startup_message_sent = False
running = True

# Robust headers for SofaScore anti-bot measures
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Encoding': 'gzip, deflate, br',
    'Accept-Language': 'en-US,en;q=0.9',
    'Connection': 'keep-alive',
    'Referer': 'https://www.sofascore.com/',
    'Origin': 'https://www.sofascore.com'
}


def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        params = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown", "disable_web_page_preview": True}
        r = standard_requests.get(url, params=params, timeout=20)
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
            data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "Markdown"}
            r = standard_requests.post(url, data=data, files=files, timeout=20)
            r.raise_for_status()
        return True
    except FileNotFoundError:
        return send_telegram(caption)
    except Exception as e:
        logging.error("Photo send error: %s", e)
        return False


def parse_sofascore_match(match):
    """Parses the raw SofaScore API JSON into a simple format."""
    home = match.get("homeTeam", {}).get("name", "Home")
    away = match.get("awayTeam", {}).get("name", "Away")
    
    gh = match.get("homeScore", {}).get("current", 0)
    ga = match.get("awayScore", {}).get("current", 0)
    
    if gh is None: gh = 0
    if ga is None: ga = 0
        
    status = match.get("status", {}).get("description", "In Play")
    league = match.get("tournament", {}).get("name", "Unknown League")
    category = match.get("tournament", {}).get("category", {}).get("name", "")
    full_league_name = f"{category} - {league}" if category else league
    
    match_id = match.get("id")
    
    return {
        "id": match_id,
        "home": home,
        "away": away,
        "gh": int(gh),
        "ga": int(ga),
        "status": status,
        "league": full_league_name
    }


def fetch_live_matches():
    if TEST_MODE:
        logging.info(f"TEST_MODE is ON. Loading matches from {TEST_FILE}...")
        try:
            if not os.path.exists(TEST_FILE):
                logging.error(f"Error: Test file '{TEST_FILE}' not found. Please create it.")
                return []

            with open(TEST_FILE, 'r') as f:
                data = json.load(f)
            
            matches = data.get('events', [])
            logging.info(f"Successfully loaded {len(matches)} matches from test file.")
            return matches
            
        except json.JSONDecodeError:
            logging.error(f"Error: Test file '{TEST_FILE}' is not valid JSON.")
            return []
        except Exception as e:
            logging.error(f"Error reading test file: {e}")
            return []

    # --- LIVE API REQUEST (Only runs if TEST_MODE is False) ---
    logging.info("Fetching live matches from SofaScore API...")
    try:
        response = requests.get(
            SOFASCORE_API_URL, 
            headers=HEADERS, 
            impersonate="chrome120", 
            timeout=20
        )
        
        response.raise_for_status() 

        data = response.json()
        
        matches = data.get('events', [])
        logging.info(f"Successfully fetched {len(matches)} live matches.")
        return matches

    # --- UPDATED EXCEPTION HANDLING BLOCK ---
    # Catch RequestException, which includes Timeout, ConnectionError, etc.
    except cffi_exceptions.RequestException as e: 
        if isinstance(e, cffi_exceptions.Timeout):
             logging.error(f"Error fetching live matches: Connection timed out after 20s. Anti-bot or network issue.")
        elif hasattr(e, 'response') and e.response and e.response.status_code == 403:
            logging.error("SofaScore blocked the request (403) even with impersonation. Anti-bot measures are very high.")
        else:
            logging.error(f"Generic error fetching live matches: {e}")
        return []
    # Catch any unexpected, non-request related errors
    except Exception as e:
        logging.error(f"An unexpected error occurred during fetch: {e}")
        return []
    # --- END UPDATED EXCEPTION HANDLING BLOCK ---


def format_startup_message(matches):
    """Limits the startup message to the top 10 matches to avoid Telegram's 400 error."""
    
    top_matches = matches[:10] 
    
    if not top_matches:
        return "⚡ *Live Matches at Startup*\n\n_No live matches found on SofaScore._"
    
    lines = ["⚡ *Top 10 Live Matches at Startup*\n"]
    for m in top_matches:
        data = parse_sofascore_match(m)
        lines.append(f"*{data['league']}*")
        lines.append(f"{data['home']} *{data['gh']}* - *{data['ga']}* {data['away']} — {data['status']}")
        lines.append("")
        
    if len(matches) > len(top_matches):
        lines.append(f"_{len(matches)} total live matches found. Check the next alert for 3-0 scores._")
    else:
        lines.append(f"_{len(matches)} total live matches found._")
        
    return "\n".join(lines)


def check_for_3goals_and_alert(matches):
    for m in matches:
        data = parse_sofascore_match(m)
        match_id = data["id"]
        gh = data["gh"]
        ga = data["ga"]
        home = data["home"]
        away = data["away"]
        status = data["status"]
        
        if not match_id:
            continue

        if ((gh == 3 and ga == 0) or (gh == 0 and ga == 3)) and match_id not in notified_matches:
            scorer = home if gh == 3 else away
            
            # --- UPDATED CAPTION ---
            caption = (
                f"⚽ *GOAL ALERT!*\n\n"
                f"{scorer} now leads *3 - 0*!\n\n"
                f"{home} *{gh}* - *{ga}* {away}\n"
                f"⏱️ **{status}**\n\n"
                f"🏆 {data['league']}\n"
                f"🔥 *Stake Now!* 🔥\n"
                f"_Avoid: Gibraltar, 2 Bundesliga, U-anything, Championship, Arab Leagues, too many goals too fast_"
            )
            
            if send_telegram_photo("stake_now_small.jpg", caption):
                logging.info(f"Alert sent for {home} vs {away}")
                notified_matches.add(match_id)


def handle_exit(signum, frame):
    global running
    logging.info("Shutting down bot...")
    running = False


signal.signal(signal.SIGINT, handle_exit)
signal.signal(signal.SIGTERM, handle_exit)


def run_bot():
    global startup_message_sent, running
    logging.info("Bot started (Curl_Cffi Mode).")
    
    while running:
        matches = fetch_live_matches()
        
        # 1. Check for 3-0 goals and ALERT
        check_for_3goals_and_alert(matches) 
        
        if not startup_message_sent:
            msg = format_startup_message(matches)
            send_telegram(msg)
            startup_message_sent = True
            
            # 2. THEN, add all 3-0 matches to notified_matches
            for m in matches:
                data = parse_sofascore_match(m)
                if (data['gh'] == 3 and data['ga'] == 0) or (data['gh'] == 0 and data['ga'] == 3):
                    notified_matches.add(data['id'])
        
        # 3. Sleep loop 
        logging.info(f"Sleeping for {POLL_INTERVAL} seconds...")
        for _ in range(POLL_INTERVAL):
            if not running:
                break
            time.sleep(1)


if __name__ == "__main__":
    run_bot()
