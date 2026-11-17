# Jio Gemini Activation Scanner (plus build)

Fork of [OshekharO/Jio-Gemini-Activation-Scanner](https://github.com/OshekharO/Jio-Gemini-Activation-Scanner)
that fixes the two things that broke it: a `panels.txt` that rots and a hardcoded
assumption that every Firebase panel speaks one schema.

## What was wrong upstream

Verified 2026-11-07 by running the original as-published:

- **131 of 146 panels were dead.** The databases return HTTP 423
  `"The Firebase database '<id>' has been deactivated"`. Upstream labels that
  `Locked` (the requests reason phrase) and moves on. Panels rot within weeks.
- **The surviving panels do not speak the expected schema.** Upstream only
  understands `{deviceId: {status: true, simInfo: {phoneNumber}}}`. Every live
  panel found uses one of two other shapes, so a live panel still produced 0
  devices and 0 numbers.
- Consequence: `python Jio.py` printed `No Online Devices Found` and exited.

Plus the usual: no 429 handling (Jio throttles with `CAPTCHA_REQUIRED` after a
handful of calls, which upstream reports as `not_jio_number`), output paths
defined twice (the CWD-relative one silently won), one `requests.Session` shared
across threads, `<table>` re-read per link, an OTP window of 15s against
forwarders that take 30-90s, and README defaults that disagreed with the code.

## What this build does instead

**Panels are harvested and probed, never trusted.**

`panel_discovery.py` merges the seed file, the upstream lists, every fork and
GitHub search into ~1000 candidates, retries dead `*.firebaseio.com` names on all
six regional `*.firebasedatabase.app` hosts (that alone revived 48 databases the
original could not see), and keeps only databases that actually answer
`/clients.json`. Results land in `panel_cache.json` with a 6h TTL; dead names are
remembered for 24h so refreshes stay fast.

**Panels are used by capability, not by assumption.**

Three schema families exist in the wild and all three are supported:

| family | `/clients` record | online signal | number keys |
|---|---|---|---|
| `status` | `{status: bool, simInfo: {phoneNumber}}` | `status == true` | `simInfo.phoneNumber` |
| `action` | `{action: {address, body, …}}` | inbox freshness | `action.address`, `cleanNumber` |
| `command` | `{pushId: {message, …}}` | inbox freshness | `phone`, `mobile`, `msisdn`, `to` |

Devices are ranked by inbox recency, numbers by a weighted score (SIM fields >
context phrases > bare 10-digit matches) with a freshness and sender bonus, and
a shared OTP box is treated as proof of ownership: candidates are tried until one
OTP verifies.

## Install

```bash
pip install -r requirements.txt
```

## Usage

```bash
# refresh the panel list from the network, then farm links
python Jio.py --refresh --sweeps 6

# quick check of what is currently reachable
python panel_discovery.py --refresh --probe-clients --print

# pin a panel list instead of using discovery
python Jio.py --panels my_panels.txt

# stop after three numbers, wider OTP window
python Jio.py --max-numbers 3 --otp-timeout 120
```

| flag | meaning |
|---|---|
| `--refresh` | re-harvest panels before running |
| `--sweeps N` / `--sweep-gap S` | repeat the device scan N times, S seconds apart |
| `--fresh-min N` | only consider inboxes fresher than N minutes |
| `--otp-timeout S` | OTP wait window (default 90) |
| `--captcha-wait S` | wait out a Jio throttle instead of skipping (default 240) |
| `--max-numbers N` | stop after N numbers |
| `--write-panels` | dump the discovered list to `panels.txt` and exit |

Outputs: `gemini_activation_links.txt`, `gemini_results.csv`,
`seen_numbers.txt` (cross-run dedupe), `panel_cache.json`.

## Reality check on the Jio side

The carrier API is the flaky half, not the Firebase half:

- `POST /login/sendOtp` answers `SEND_OTP_CURRENTLY_LOCKED` for a number whose
  OTP was requested recently — expected, retried later, not a failure of the tool.
- `GET /recharge/mobility/number/{mobile}` starts returning
  `429 CAPTCHA_REQUIRED` after roughly 4-8 calls and recovers on its own after
  one to four minutes. The scanner waits it out (`--captcha-wait`).
- `www.jio.com` intermittently stalls a whole timeout on a cold connection, so
  every call retries once.
- The final offer endpoints are Jio's and get reshuffled between releases.
  Verified live on 2026-11-17, with a session where `authJsonData` reports
  `loginFlag=true` and `navigate/Z0241` returns 200:

  | endpoint | answer |
  |---|---|
  | `subscription/activate/googleai` | 200 `{}` |
  | `subscription/google-lead` | 200 `LEAD_GENERATED` |
  | `subscription/submit?type=Z0241&source=JIO` | 200 `SUCCESS` |
  | `submission/submit` (upstream's path) | 404, moved |
  | `subscription/google-ai` | **401 `Not Authorized`** |
  | `subscription/activate/Z0241?source=JIO` | **401 `Not Authorized`** |

  `google-ai` is the one that used to return `redirectionURL`, so a 401 there is
  why this fork can log in and still report `activation_api_failed`. It is not a
  header problem: the same 401 comes from a real logged-in Chrome, from page
  context, and for every documented variant (POST, query params, `sso-token` /
  `Authorization` / `uniqueId` headers, `x-request-source`). It looks like an
  account/entitlement check on Jio's side, and the offer page itself currently
  403s and redirects to `page-not-found.html`.

  `claim_activation()` therefore walks all of them, in GET and POST form, and
  accepts a `serviceactivation.google.com` token URL, a `one.google.com` promo
  URL, or a coupon code — whichever appears first.

- Because of the above, expect this sequence: **log in -> `google-lead` +
  `submit` succeed -> `google-ai` decides whether you get a link.** When Jio
  restores that endpoint the fork picks the link up with no code change, since
  the response is scanned for either URL family.

Expect a link to require patience regardless: a shared forwarder number is often
locked by whoever else farms it. `--sweeps` exists for that — every sweep only
looks at fresh inboxes, so links arrive as phones buzz.

## Tests

```bash
python tests.py     # 99 checks, no network
```

Covers the logic that decided whether the tool works on live data: panel parsing,
regional host expansion, schema classification, message flattening, number
scoring, OTP extraction, link/coupon harvesting, the activation fallback chain
(with a fake HTTP session) and output dedupe.

## Layout

| file | role |
|---|---|
| `Jio.py` | scanner: discovery, schema adapters, OTP, claim |
| `tests.py` | 99 offline checks for the parsing/scoring/claim logic |
| `panel_discovery.py` | harvest, regional retry, liveness probe, cache (`--refresh`) |
| `panels_seed.txt` | hand-picked survivors used as a discovery base |
| `panels.txt` | optional pinned list (`url|api_key|schema`) |

## Credit

Flow and the Jio endpoint set come from OshekharO's original. Schema adapters,
discovery, retry and pacing are from this fork.

Educational use; the databases belong to third parties.
