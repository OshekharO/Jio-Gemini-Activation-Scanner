"""
Panel discovery + liveness probing for Jio Gemini scanners.

Panels are public Firebase Realtime Database instances used by SMS-forwarder
apps. They die constantly (project deleted -> HTTP 423 "deactivated"), so a
static panels.txt rots within weeks. This module keeps the list fresh:

  1. multi-source harvest  - official panels.txt, forks, and GitHub search
  2. regional host retry   - dead *.firebaseio.com names are re-tried on every
                             regional *.firebasedatabase.app host, which
                             revives them when only the legacy domain dies
  3. liveness probe        - keep only databases that actually answer /clients.json
  4. on-disk cache         - short TTL, lets offline/CI runs reuse the last good list

Every probe is a cheap unauthenticated GET of /clients.json. Nothing is written.

CLI:
    python panel_discovery.py --refresh           # rebuild the cache
    python panel_discovery.py --refresh --probe-clients
    python panel_discovery.py --print --limit 20
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

import requests

BASE_DIR = Path(__file__).resolve().parent
CACHE_FILE = BASE_DIR / "panel_cache.json"
SEED_FILE = BASE_DIR / "panels_seed.txt"
PANELS_FILE = BASE_DIR / "panels.txt"

CACHE_TTL = 6 * 3600          # reuse a fresh cache instead of re-harvesting
DEAD_TTL = 24 * 3600          # don't re-probe known-dead panels for a day
PROBE_WORKERS = 48
PROBE_TIMEOUT = 8
REGION_WORKERS = 32

UPSTREAM_RAW = (
    "https://raw.githubusercontent.com/{repo}/{branch}/panels.txt"
)
PANEL_SOURCES = (
    "OshekharO/Jio-Gemini-Activation-Scanner",
    "75Tushar/Jio-Gemini-Activation-Scanner",
    "ErisaiKalimon/Jio-Gemini-Activation-Scanner",
    "thanhnam007/Jio-Gemini-Activation-Scanner",
    "auzt/Jio-Gemini-Activation-Scanner",
    "kaisardesa28/jio",
)
FORK_API = (
    "https://api.github.com/repos/OshekharO/"
    "Jio-Gemini-Activation-Scanner/forks?sort=newest&per_page=40"
)
SEARCH_API = (
    "https://api.github.com/search/repositories"
    "?q=jio+gemini+in:name,description&per_page=30&sort=updated"
)

REGIONS = (
    "us-central1",
    "asia-southeast1",
    "europe-west1",
    "us-west1",
    "asia-south1",
    "northamerica-northeast1",
)

HEADERS = {
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
    ),
}

# Envelope written by the scanner; several families exist, so a panel is only
# "usable" once we know which one it speaks. See README.md#panel-schemas.
SCHEMA_STATUS = "status"      # {id: {"status": true, "simInfo": {"phoneNumber": ...}}}
SCHEMA_ACTION = "action"      # {id: {"action": {"cleanNumber": ...}}}
SCHEMA_COMMAND = "command"    # {id: {"<push>": {"message": ...}}}  (inbox-only)


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def _normalize_url(url: str) -> str:
    url = (url or "").strip().replace(" ", "")
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url.rstrip("/")


def is_firebase_rtdb(url: str) -> bool:
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return False
    return host.endswith("firebaseio.com") or host.endswith("firebasedatabase.app")


def parse_panel_line(line: str) -> tuple[str, str] | None:
    """`url|api_key` -> (url, api_key). Returns None for junk/comment lines."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    head, _, tail = line.partition("|")
    url = _normalize_url(head)
    if not url or not is_firebase_rtdb(url):
        return None
    key = tail.strip().replace(" ", "")
    if key.startswith("http"):
        key = ""
    return url, key


def parse_panel_text(text: str) -> list[tuple[str, str]]:
    return [p for p in (parse_panel_line(l) for l in text.splitlines()) if p]


def candidates(url: str) -> list[str]:
    """Legacy *.firebaseio.com names also resolve on regional hosts."""
    if "firebasedatabase.app" in url:
        return [url]
    project = (urlsplit(url).hostname or "").split(".firebaseio.com")[0]
    if not project:
        return [url]
    # The literal US region first: it is the cheapest 423 for deactivated DBs.
    hosts = [f"https://{project}.us-central1.firebasedatabase.app"]
    hosts += [f"https://{project}.{r}.firebasedatabase.app" for r in REGIONS]
    out = [url]
    for host in hosts:
        if host not in out:
            out.append(host)
    return out


# --------------------------------------------------------------------------- #
# harvest
# --------------------------------------------------------------------------- #
def _http(url: str, timeout: int = 15, params: dict[str, Any] | None = None) -> Any:
    try:
        with requests.Session() as session:
            response = session.get(url, params=params, timeout=timeout, headers=HEADERS)
            if response.status_code != 200:
                return None
            return response
    except requests.RequestException:
        return None


def _gh(url: str) -> Any:
    response = _http(url)
    if response is None:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def _raw_panels(repo: str) -> list[tuple[str, str]]:
    for branch in ("main", "master"):
        response = _http(UPSTREAM_RAW.format(repo=repo, branch=branch))
        if response is not None:
            return parse_panel_text(response.text)
    return []


def harvest(verbose: bool = True) -> dict[str, str]:
    """url -> api_key, merged seed file + upstream lists + forks + search."""
    merged: dict[str, str] = {}

    def add(url: str, key: str) -> None:
        url = _normalize_url(url)
        if url and is_firebase_rtdb(url):
            if not merged.get(url):
                merged[url] = key

    if SEED_FILE.exists():
        for url, key in parse_panel_text(SEED_FILE.read_text(encoding="utf-8")):
            add(url, key)

    for repo in PANEL_SOURCES:
        for url, key in _raw_panels(repo):
            add(url, key)

    forks = _gh(FORK_API) or []
    if isinstance(forks, list):
        for fork in forks:
            full = fork.get("full_name") if isinstance(fork, dict) else None
            if full:
                for url, key in _raw_panels(full):
                    add(url, key)

    found = _gh(SEARCH_API) or {}
    if isinstance(found, dict):
        for item in found.get("items", []):
            full = item.get("full_name") if isinstance(item, dict) else None
            if full:
                for url, key in _raw_panels(full):
                    add(url, key)

    if verbose:
        print(f"harvested {len(merged)} unique panel URLs", file=sys.stderr)
    return merged


# --------------------------------------------------------------------------- #
# probe
# --------------------------------------------------------------------------- #
def _classify(data: Any) -> str | None:
    if not isinstance(data, dict) or not data:
        return None
    for value in data.values():
        if not isinstance(value, dict):
            continue
        if "status" in value or "simInfo" in value:
            return SCHEMA_STATUS
        if "action" in value:
            return SCHEMA_ACTION
        if any(isinstance(v, dict) and "message" in v for v in value.values()):
            return SCHEMA_COMMAND
    # Unknown but populated: treat as command/inbox family so it is still usable.
    return SCHEMA_COMMAND


def _record(host: str, key: str, data: dict[str, Any], probe_clients: bool) -> dict[str, Any]:
    online = 0
    if probe_clients:
        for value in data.values():
            if not isinstance(value, dict):
                continue
            status = value.get("status")
            if status is True or str(status).lower() == "true":
                online += 1
    return {
        "url": host,
        "key": key,
        "schema": _classify(data),
        "clients": len(data),
        "online": online,
        "probed_at": time.time(),
    }


def probe_host(host: str, key: str, probe_clients: bool) -> tuple[bool, Any]:
    """(answered, record) — `answered` is False for 423/404/network, so the
    caller knows to retry the same database on a regional host."""
    params = {"auth": key} if key else {}
    response = _http(f"{host}/clients.json", params=params, timeout=PROBE_TIMEOUT)
    if response is None:
        return False, None
    try:
        data = response.json()
    except ValueError:
        return False, None
    if not isinstance(data, dict):
        return False, None
    return True, _record(host, key, data, probe_clients)


def probe_all(
    entries: Iterable[tuple[str, str]], probe_clients: bool = False
) -> list[dict[str, Any]]:
    """Probe originals in parallel, then only the misses on regional hosts."""
    live, _answered = probe_all_sourced(entries, probe_clients=probe_clients)
    return live


def probe_all_sourced(
    entries: Iterable[tuple[str, str]], probe_clients: bool = False
) -> tuple[list[dict[str, Any]], set[str]]:
    """Same as probe_all, plus the set of original URLs that answered."""
    entries = list(entries)
    live: list[dict[str, Any]] = []
    answered_originals: set[str] = set()
    misses: list[tuple[str, str]] = []

    def original_try(entry: tuple[str, str]):
        url, key = entry
        answered, record = probe_host(url, key, probe_clients)
        return answered, record, entry

    with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
        for future in as_completed([pool.submit(original_try, e) for e in entries]):
            try:
                answered, record, entry = future.result()
            except Exception:
                continue
            if answered and record:
                live.append(record)
                answered_originals.add(entry[0])
            else:
                misses.append(entry)

    region_map: dict[tuple[str, str], str] = {}
    for url, key in misses:
        for host in candidates(url)[1:]:
            region_map[(host, key)] = url
    if region_map:
        seen_hosts: set[str] = set()
        with ThreadPoolExecutor(max_workers=REGION_WORKERS) as pool:
            futures = {pool.submit(probe_host, h, k, probe_clients): (h, k)
                       for h, k in region_map}
            for future in as_completed(futures):
                try:
                    answered, record = future.result()
                except Exception:
                    continue
                if not answered or not record or record["url"] in seen_hosts:
                    continue
                seen_hosts.add(record["url"])
                live.append(record)
                answered_originals.add(region_map[futures[future]])

    live.sort(key=lambda r: (-r.get("online", 0), -r.get("clients", 0), r["url"]))
    return live, answered_originals


# --------------------------------------------------------------------------- #
# cache-facing API
# --------------------------------------------------------------------------- #
def load_cache() -> dict[str, Any]:
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_cache(panels: list[dict[str, Any]], stats: dict[str, Any],
               dead: dict[str, float] | None = None) -> None:
    CACHE_FILE.write_text(
        json.dumps({"ts": time.time(), "stats": stats, "panels": panels,
                    "dead": dead or {}}, indent=1),
        encoding="utf-8",
    )


def cache_is_fresh(cache: dict[str, Any], ttl: int = CACHE_TTL) -> bool:
    panels = cache.get("panels")
    if not isinstance(panels, list) or not panels:
        return False
    return (time.time() - float(cache.get("ts", 0))) < ttl


def as_entries(panels: Iterable[dict[str, Any]]) -> list[tuple[str, str, str]]:
    """(url, api_key, schema) — used directly by the scanner."""
    return [(p["url"], p.get("key", ""), p.get("schema") or SCHEMA_COMMAND) for p in panels]


def get_panels(
    refresh: bool = False,
    probe_clients: bool = False,
    verbose: bool = True,
    ttl: int = CACHE_TTL,
    recheck_dead: bool = False,
) -> list[tuple[str, str, str]]:
    """Fresh cached panels, or a fresh harvest. Falls back to a stale cache.

    Known-dead panels are remembered, so repeat refreshes only probe the new
    names plus anything dead for longer than DEAD_TTL.
    """
    cache = load_cache()
    if not refresh and cache_is_fresh(cache, ttl):
        if verbose:
            age = (time.time() - float(cache.get("ts", 0))) / 60
            print(f"panel cache: {len(cache['panels'])} panels, {age:.0f}m old", file=sys.stderr)
        return as_entries(cache["panels"])

    harvested = harvest(verbose=verbose)
    dead: dict[str, float] = dict(cache.get("dead") or {})
    now = time.time()
    candidates: dict[str, str] = {}
    for url, key in harvested.items():
        if not recheck_dead and now - float(dead.get(url, 0)) < DEAD_TTL:
            continue
        candidates[url] = key
    if verbose:
        print(f"probe: {len(candidates)} of {len(harvested)} candidates "
              f"({len(harvested) - len(candidates)} known-dead skipped)", file=sys.stderr)

    live, answered = probe_all_sourced(candidates.items(), probe_clients=probe_clients)
    for url in candidates:
        if url in answered:
            dead.pop(url, None)
        else:
            dead[url] = now
    dead = {u: ts for u, ts in dead.items() if now - ts < 30 * 86400}
    stats = {"harvested": len(harvested), "probed": len(candidates), "live": len(live)}
    if verbose:
        print(
            f"probe: {len(candidates)} candidates -> {len(live)} live "
            f"(was {len(cache.get('panels', []))})",
            file=sys.stderr,
        )
    if live:
        save_cache(live, stats, dead)
        return as_entries(live)
    if cache.get("panels"):
        if verbose:
            print("probe found nothing; reusing stale cache", file=sys.stderr)
        return as_entries(cache["panels"])
    return []


def write_panels_file(panels: list[tuple[str, str, str]], path: Path = PANELS_FILE) -> None:
    header = (
        "# Auto-generated by panel_discovery.py — re-run `--refresh` instead of editing.\n"
        "# format: url|api_key|schema   (schema is filled by the scanner)\n"
    )
    body = "".join(f"{u}|{k}\n" for u, k, _ in sorted(panels))
    path.write_text(header + body, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Harvest + probe Jio panel databases")
    ap.add_argument("--refresh", action="store_true", help="force a new harvest")
    ap.add_argument("--probe-clients", action="store_true",
                    help="also count online devices during the probe (slower)")
    ap.add_argument("--print", action="store_true", dest="do_print", help="print live panels")
    ap.add_argument("--limit", type=int, default=0, help="cap printed rows")
    ap.add_argument("--write", action="store_true", help="write panels.txt")
    ap.add_argument("--recheck-dead", action="store_true",
                    help="also re-probe panels already known to be dead")
    args = ap.parse_args()

    panels = get_panels(refresh=args.refresh, probe_clients=args.probe_clients,
                        recheck_dead=args.recheck_dead)
    if not panels:
        print("no live panels found", file=sys.stderr)
        return 1
    if args.do_print:
        rows = panels[: args.limit] if args.limit else panels
        for url, _key, schema in rows:
            print(f"{schema:<8} {url}")
    if args.write:
        write_panels_file(panels)
        print(f"wrote {len(panels)} panels -> {PANELS_FILE}", file=sys.stderr)
    if not args.do_print and not args.write:
        print(f"{len(panels)} live panels cached -> {CACHE_FILE}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
