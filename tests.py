"""Self-tests for the scanner's pure logic. Run: python tests.py

Covers the parts that actually decided whether the tool works on live data:
panel-line parsing, regional host expansion, schema classification, number
scoring/ranking, OTP extraction and link harvesting. No network access needed.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import Jio
import panel_discovery as discovery

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
        FAILURES.append(label)


def check_true(label: str, got) -> None:
    check(label, bool(got), True)


def test_panel_parsing() -> None:
    print("panel parsing")
    check("url|key",
          discovery.parse_panel_line("https://a-default-rtdb.firebaseio.com|AIzaKEY"),
          ("https://a-default-rtdb.firebaseio.com", "AIzaKEY"))
    check("key optional, spaces stripped",
          discovery.parse_panel_line("  https://b-default-rtdb.firebaseio.com | k1 "),
          ("https://b-default-rtdb.firebaseio.com", "k1"))
    check("scheme added", discovery.parse_panel_line("c-default-rtdb.firebaseio.com"),
          ("https://c-default-rtdb.firebaseio.com", ""))
    check("non-firebase rejected", discovery.parse_panel_line("https://x.com|k"), None)
    check("comment ignored", discovery.parse_panel_line("# hi"), None)
    check("blank ignored", discovery.parse_panel_line("   "), None)
    check("header url key ignored",
          discovery.parse_panel_line("https://d.firebaseio.com|http://nope"),
          ("https://d.firebaseio.com", ""))
    check("regional host kept",
          discovery.parse_panel_line(
              "https://e-default-rtdb.asia-southeast1.firebasedatabase.app|k"),
          ("https://e-default-rtdb.asia-southeast1.firebasedatabase.app", "k"))


def test_region_expansion() -> None:
    print("regional host expansion")
    hosts = discovery.candidates("https://proj-default-rtdb.firebaseio.com")
    check("original first", hosts[0], "https://proj-default-rtdb.firebaseio.com")
    check_true("us-central1 retried",
               "https://proj-default-rtdb.us-central1.firebasedatabase.app" in hosts)
    check_true("asia-southeast1 retried",
               "https://proj-default-rtdb.asia-southeast1.firebasedatabase.app" in hosts)
    check("no duplicates", len(hosts), len(set(hosts)))
    check("regional url is not expanded",
          discovery.candidates("https://p.africa-south1.firebasedatabase.app"),
          ["https://p.africa-south1.firebasedatabase.app"])


def test_schema_classification() -> None:
    print("schema classification")
    check("status family", discovery._classify({"d": {"status": True, "simInfo": {}}}),
          discovery.SCHEMA_STATUS)
    check("action family", discovery._classify({"d": {"action": {"address": "+91"}}}),
          discovery.SCHEMA_ACTION)
    check("command family", discovery._classify({"d": {"-P1": {"message": "hi"}}}),
          discovery.SCHEMA_COMMAND)
    check("empty is no family", discovery._classify({}), None)
    check("unknown populated falls back", discovery._classify({"d": {"weird": 1}}),
          discovery.SCHEMA_COMMAND)


def test_message_helpers() -> None:
    print("message helpers")
    check("body from 'message'", Jio.message_body({"message": "hi"}), "hi")
    check("body from nested", Jio.message_body({"data": {"body": "deep"}}), "deep")
    check("body from 'msg'", Jio.message_body({"msg": "m"}), "m")
    check("empty body", Jio.message_body({"foo": 1}), "")
    check("time from field", Jio.message_time("k", {"timestamp": 1700000000000}),
          1700000000000.0)
    check("time from keypath", Jio.message_time("-P1/1700000000000", {}), 1700000000000.0)
    check("no time", Jio.message_time("-P1", {}), 0.0)
    flat = Jio.flatten_messages({"-P1": {"message": "a"}, "-P2": {"message": "b"}})
    check("flat messages", [k for k, _ in flat], ["-P1", "-P2"])
    nested = Jio.flatten_messages({"dev1": {"-P1": {"message": "a"}}})
    check("nested messages", [k for k, _ in nested], ["dev1/-P1"])
    check("non-dict ignored", Jio.flatten_messages({"d": "scalar"}), [])


def test_number_normalisation() -> None:
    print("number normalisation")
    check("plain 10", Jio.normalize_mobile("9876543210"), "9876543210")
    check("with 91", Jio.normalize_mobile("+919876543210"), "9876543210")
    check("with spaces", Jio.normalize_mobile("+91 98765 43210"), "9876543210")
    check("leading 0 91", Jio.normalize_mobile("00919876543210"), "9876543210")
    check("landline rejected", Jio.normalize_mobile("1234567890"), None)
    check("short rejected", Jio.normalize_mobile("98765"), None)
    check("none safe", Jio.normalize_mobile(None), None)


def test_number_scoring() -> None:
    print("number scoring")
    fresh = time.time() * 1000
    stale = fresh - 30 * 3600 * 1000
    items = [
        ("a", {"message": "Your Jio number 9000000001 plan expires", "timestamp": stale}),
        ("b", {"message": "Some random text 9000000002", "timestamp": fresh}),
    ]
    scores = dict(Jio.number_scores(items))
    check_true("context beats fresher bare match",
               scores["9000000001"] > scores["9000000002"])
    check_true("only 10-digit patterns kept", all(len(n) == 10 for n in scores))

    sim_items = [("a", {"phoneNumber": "9876543210", "message": "hello"})]
    check_true("sim field dominates", dict(Jio.number_scores(sim_items))["9876543210"] >= 10)

    o1 = [("a", {"message": "123456 is your OTP for 9111111111 on www.jio.com",
                 "sender": "JM-JIONET-S", "timestamp": fresh})]
    o2 = [("a", {"message": "998877 is your Swiggy OTP for 9222222222",
                 "timestamp": fresh})]
    check_true("Jio sender outranks generic sender",
               Jio.number_scores(o1)[0][1] > Jio.number_scores(o2)[0][1])
    check("candidate order deterministic",
          [n for n, _ in Jio.number_scores(items)], ["9000000001", "9000000002"])

    same_ctx_stale = [("a", {"message": "Jio number 9000000004", "timestamp": stale})]
    same_ctx_fresh = [("a", {"message": "Jio number 9000000004", "timestamp": fresh})]
    check_true("freshness breaks ties at equal context",
               Jio.number_scores(same_ctx_fresh)[0][1] > Jio.number_scores(same_ctx_stale)[0][1])


def test_otp_extraction() -> None:
    print("otp extraction")
    check_true("otp word detected",
               Jio.OTP_WORD_PATTERN.search("418501 is your One time password (OTP) to sign-in"))
    check_true("verification code detected",
               Jio.OTP_WORD_PATTERN.search("Your verification code is 123456"))
    check("6 digits extracted",
          Jio.OTP_PATTERN.search("418501 is your OTP").group(1), "418501")
    check("10 digits not treated as otp",
          Jio.OTP_PATTERN.search("id 1234567890 OTP"), None)
    check("jio rank beats generic",
          Jio.otp_rank("OTP to sign-in on www.jio.com", "JM-JIONET-S"),
          3)
    check("generic rank", Jio.otp_rank("Your Swiggy OTP", "SWIGGY"), 1)
    check("no rank without otp word/sender", Jio.otp_rank("hello", ""), 0)


def test_link_harvesting() -> None:
    print("link harvesting")
    token = "A" * 60
    payload = f'{{"redirectionURL":"https://serviceactivation.google.com/subscription/new/{token}"}}'
    check_true("token url extracted", Jio.activation_url(payload) .startswith(
        "https://serviceactivation.google.com/subscription/new/AAAA"))
    check_true("html escaped url decoded",
               Jio.activation_url(f"x&amp;y: https://serviceactivation.google.com/subscription/new/{token}"))
    check_true("url-encoded url decoded",
               Jio.activation_url(
                   f"https%3A%2F%2Fserviceactivation.google.com%2Fsubscription%2Fnew%2F{token}"))
    check_true("padding preserved",
               Jio.activation_url(
                   f"https://serviceactivation.google.com/subscription/new/{token}==")
               .endswith("AA=="))
    check_true("short token rejected",
               Jio.activation_url("https://serviceactivation.google.com/subscription/new/short") == "")
    promo = "https://one.google.com/activate-plan?token=abc123"
    check("promo url", Jio.promo_url(promo), promo)
    check("coupon code", Jio.coupon_code("Use coupon CODE: GOOG-18MO now"),
          "GOOG-18MO")
    check("no coupon", Jio.coupon_code("nothing here"), "")
    check("harvest from dict key",
          Jio.harvest_link({"redirectionURL": promo}, ""), promo)
    check("harvest from raw body",
          Jio.harvest_link({}, promo + " "), promo)


def test_response_helpers() -> None:
    print("response helpers")
    check("success body is not an error",
          Jio.api_error({"responseCode": "200", "responseMsg": "OK"}), "")
    check("error message wins", Jio.api_error({"errorMessage": "LOCKED"}), "LOCKED")
    check("bad code is an error", Jio.api_error({"responseCode": "400"}), "400")
    check("api_message still reads success text",
          Jio.api_message({"responseMsg": "OK"}), "OK")
    check_true("already active detected",
               Jio.already_active("Offer is already activated for this number"))
    check_true("already redeemed detected",
               Jio.already_active("ALREADY_REDEEMED"))
    check("fresh number not active", Jio.already_active("SUCCESS"), False)


class FakeResponse:
    def __init__(self, status_code: int = 200, body: str = "{}") -> None:
        self.status_code = status_code
        self.text = body
        self.ok = 200 <= status_code < 300

    def json(self):
        import json as _json
        try:
            return _json.loads(self.text)
        except ValueError:
            raise ValueError("no json")


class FakeSession:
    """Minimal requests.Session stand-in: url -> (status, body) map."""

    def __init__(self, routes: dict[str, tuple[int, str]], navigate_status: int = 200):
        self.routes = routes
        self.navigate_status = navigate_status
        self.calls: list[tuple[str, str]] = []
        self.headers: dict[str, str] = {}

    def _do(self, method: str, url: str, **kwargs):
        self.calls.append((method, url))
        if "navigate/Z0241" in url:
            return FakeResponse(self.navigate_status, "{}")
        status, body = self.routes.get(url, (404, '{"errorMessage":"nope"}'))
        return FakeResponse(status, body)

    def get(self, url, **kwargs):
        return self._do("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._do("POST", url, **kwargs)


def test_bind_session() -> None:
    print("session binding")
    bound = FakeSession({}, navigate_status=200)
    check_true("binds when navigate answers 200", Jio.bind_session(bound))
    stuck = FakeSession({}, navigate_status=401)
    check("gives up when navigate keeps 401", Jio.bind_session(stuck), False)
    check_true("retried navigate several times",
               sum(1 for m, u in stuck.calls if "navigate" in u) >= 3)


def test_claim_activation() -> None:
    print("claim activation")
    token = "B" * 60
    good = f"https://serviceactivation.google.com/subscription/new/{token}"
    # current live path answers with the link
    current = Jio.ACTIVATE_ATTEMPTS[0]
    session = FakeSession({current: (200, '{"redirectionURL":"%s"}' % good)})
    status, url, _coupon = Jio.claim_activation(session)
    check("status ok", status, "activation_url_found")
    check("url harvested", url, good)

    # only the legacy path answers
    legacy = Jio.ACTIVATE_ATTEMPTS[-1]
    session = FakeSession({legacy: (200, '{"redirectionURL":"%s"}' % good)})
    check("falls through to legacy path", Jio.claim_activation(session)[0],
          "activation_url_found")

    # already redeemed
    session = FakeSession({current: (200, '{"errorMessage":"Number is ALREADY_ACTIVE"}')})
    check("already active detected", Jio.claim_activation(session)[0], "already_active")

    # every endpoint answers 200 with an empty body -> no link, no crash
    session = FakeSession({u: (200, "{}") for u in Jio.ACTIVATE_ATTEMPTS})
    check("no link when all empty", Jio.claim_activation(session)[0], "no_activation_url")
    check_true("every activation endpoint tried",
               {u for _m, u in session.calls} >= set(Jio.ACTIVATE_ATTEMPTS))

    # unbound session short-circuits before hitting the offer endpoints
    session = FakeSession({}, navigate_status=401)
    check("unbound session reported", Jio.claim_activation(session)[0],
          "api_session_invalid")

    # submit is called once a link is found
    session = FakeSession({current: (200, '{"redirectionURL":"%s"}' % good)})
    Jio.claim_activation(session)
    check_true("submit attempted after success",
               any("submit" in u for _m, u in session.calls))


def test_result_rows() -> None:
    print("result rows")
    import tempfile
    saved = (Jio.RESULTS_FILE, Jio.LINKS_FILE, Jio.SEEN_FILE)
    with tempfile.TemporaryDirectory() as tmp:
        Jio.RESULTS_FILE = Path(tmp) / "r.csv"
        Jio.LINKS_FILE = Path(tmp) / "l.txt"
        Jio.SEEN_FILE = Path(tmp) / "s.txt"
        output = Jio.Output()
        device = {"panel": {"url": "https://p.firebaseio.com", "schema": "action"},
                  "device_id": "dev1"}
        output.result(1, device, "9000000001", "otp_failed", "")
        output.result(2, device, "9000000002", "activation_url_found",
                      "https://serviceactivation.google.com/subscription/new/" + "C" * 60,
                      "GOOG-18MO")
        rows = Jio.RESULTS_FILE.read_text(encoding="utf-8").splitlines()
        check("header written once", rows[0].split(",")[0], "serial_number")
        check("schema column filled", rows[1].split(",")[1], "action")
        check("panel url written", rows[1].split(",")[2], "https://p.firebaseio.com")
        check_true("coupon persisted", "GOOG-18MO" in rows[2])
        check("link dedupe", output.add_link("https://x"), True)
        check("link dedupe repeat", output.add_link("https://x"), False)
        check_true("link on disk", Jio.LINKS_FILE.read_text(encoding="utf-8").strip() == "https://x")
        check("number dedupe", output.add_number("9000000001"), None)
        check_true("number seen", output.seen_number("9000000001"))
        check_true("seen file written", "9000000001" in Jio.SEEN_FILE.read_text())
    Jio.RESULTS_FILE, Jio.LINKS_FILE, Jio.SEEN_FILE = saved


def test_runtime_config() -> None:
    print("runtime config")
    check("output paths are script-relative",
          Jio.LINKS_FILE.parent, Jio.BASE_DIR)
    check("results path too", Jio.RESULTS_FILE.parent, Jio.BASE_DIR)
    check_true("otp window longer than upstream 15s", Jio.OTP_TIMEOUT >= 60)
    check_true("several candidates per device", Jio.MAX_CANDIDATES >= 2)
    check_true("at least four activation endpoints", len(Jio.ACTIVATE_ATTEMPTS) >= 4)
    check_true("google-ai is attempted",
               any("google-ai" in u for u in Jio.ACTIVATE_ATTEMPTS))
    check_true("submit fallbacks exist", len(Jio.SUBMIT_ATTEMPTS) >= 2)
    for url in Jio.ACTIVATE_ATTEMPTS + Jio.SUBMIT_ATTEMPTS:
        check_true(f"https url: {url.rsplit('/', 1)[-1]}", url.startswith("https://"))


def main() -> int:
    for test in (
        test_panel_parsing,
        test_region_expansion,
        test_schema_classification,
        test_message_helpers,
        test_number_normalisation,
        test_number_scoring,
        test_otp_extraction,
        test_link_harvesting,
        test_response_helpers,
        test_bind_session,
        test_claim_activation,
        test_result_rows,
        test_runtime_config,
    ):
        test()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
