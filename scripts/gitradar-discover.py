#!/usr/bin/env python3
"""
GitHub Radar — Automated Repo Discovery Pipeline (v4.1.0, Community Edition)
Dual-mode: daily (lightweight new finds) + weekly (full re-evaluation).

Daily mode (default, `--mode daily`):
- Queries GitHub Search API for repos pushed in last 7 days
- Deduplicates against cache (14-day TTL)
- Scrapes GitHub Trending (daily + weekly pages) as a primary source
- Outputs: clean text summary to stdout + discoveries.json

Weekly mode (`--mode weekly`):
- Full re-scan without cache dedup
- Compares each found repo's pushed_at against cached last_pushed
- Flags repos with new activity since last weekly check
- Refreshes cache with updated pushed_at timestamps
- Outputs: clean text summary + discoveries.json

v4.1.0 — Dual-mode, pushed_at tracking, trending as primary source.
"""

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import uuid
import time
import fcntl
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone, timedelta

# ── Mode ─────────────────────────────────────────────────────────────

MODE = "daily"  # default, overridden by --mode weekly
if "--mode" in sys.argv:
    idx = sys.argv.index("--mode")
    if idx + 1 < len(sys.argv):
        MODE = sys.argv[idx + 1]
if "--help" in sys.argv or "-h" in sys.argv:
    print(f"Usage: {sys.argv[0]} [--mode daily|weekly]")
    print()
    print("  --mode daily   (default) Lightweight new-find scan with cache dedup")
    print("  --mode weekly  Full re-evaluation: re-scans cached repos and flags new activity")
    print()
    print("Outputs:")
    print("  - Clean human-readable summary to stdout")
    print("  - data/discoveries.json — full structured results")
    sys.exit(0)

# ── Data Paths ──────────────────────────────────────────────────────

# Always use local data/ directory for community edition
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
CACHE_FILE = os.path.join(DATA_DIR, "cache.json")
OUTPUT_FILE = os.path.join(DATA_DIR, "discoveries.json")
METRICS_FILE = os.path.join(DATA_DIR, "metrics.json")
THRESHOLDS_FILE = os.path.join(DATA_DIR, "thresholds.json")

DATE_STR = datetime.now(timezone.utc).strftime("%Y-%m-%d")
TIME_STR = datetime.now(timezone.utc).strftime("%H:%M")

# ── Durable publication ─────────────────────────────────────────────
#
# Every state file here is read-modify-written, and daily and weekly runs
# can overlap. A plain open(path, "w") truncates before writing, so a crash
# or a competing writer could leave a half-written document that still looked
# like a completed run. Publish through a temp file in the same directory and
# os.replace() so a reader only ever sees a complete document.

_STATE_LOCK_NAME = ".state.lock"


_LOCK_STATE = threading.local()


class StateLock:
    """Reentrant exclusive advisory lock for a shared read-modify-write cycle.

    Atomic replacement stops torn files. It does not stop two writers each
    loading, modifying and saving, where the second silently discards the
    first's update.

    Reentrant within a thread: flock() is per open-file description, so a
    transaction that re-enters the lock through a helper would block on
    itself. Depth is tracked per thread; only the outermost acquisition
    touches the file lock.
    """

    def __init__(self, directory=None):
        self._dir = directory or DATA_DIR

    def __enter__(self):
        depth = getattr(_LOCK_STATE, "depth", 0)
        if depth == 0:
            os.makedirs(self._dir, exist_ok=True)
            fh = open(os.path.join(self._dir, _STATE_LOCK_NAME), "a+")
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            _LOCK_STATE.fh = fh
        _LOCK_STATE.depth = depth + 1
        return self

    def __exit__(self, *exc_info):
        depth = getattr(_LOCK_STATE, "depth", 1) - 1
        _LOCK_STATE.depth = depth
        if depth <= 0:
            fh = getattr(_LOCK_STATE, "fh", None)
            if fh is not None:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                finally:
                    fh.close()
                    _LOCK_STATE.fh = None
            _LOCK_STATE.depth = 0
        return False


def atomic_write_text(path, text):
    """Publish text atomically: temp file, fsync, replace."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path, data, indent=2):
    """Serialise and validate first, then publish atomically."""
    text = json.dumps(data, indent=indent, default=str)
    json.loads(text)
    return atomic_write_text(path, text)


# ── Committed generation ────────────────────────────────────────────
#
# A published discoveries.json is only trustworthy if it can be tied to the
# exact bytes and manifest that were committed together. The commit pointer
# is written last; a reader that finds no valid pointer must treat the file
# as uncommitted rather than assume it is current.

COMMIT_FILE = os.path.join(DATA_DIR, "discoveries-commit.json")
MANIFEST_FILE = os.path.join(DATA_DIR, "discoveries-manifest.json")
ATTEMPT_FILE = os.path.join(DATA_DIR, "discoveries-attempt.json")


def new_generation_id(mode=None, when=None):
    """Unique-per-run identity. Content is bound separately by digest."""
    when = when or datetime.now(timezone.utc)
    return f"{when.strftime('%Y%m%dT%H%M%S%fZ')}-{mode or MODE}-{uuid.uuid4().hex[:8]}"


def read_committed_discoveries(path=None):
    """Return (payload, commit) for a committed generation, else None.

    Fails closed: no commit pointer, a malformed one, a mismatched digest or
    a non-complete status all mean "not a committed result".
    """
    path = path or OUTPUT_FILE
    try:
        with open(COMMIT_FILE, encoding="utf-8") as f:
            commit = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(commit, dict) or commit.get("status") != "complete":
        return None
    try:
        payload = json.loads(open(path, encoding="utf-8").read())
    except (OSError, ValueError):
        return None
    actual = hashlib.sha256(open(path, "rb").read()).hexdigest()
    if commit.get("discoveries_sha256") and actual != commit["discoveries_sha256"]:
        return None
    return payload, commit


# ── Static Config ───────────────────────────────────────────────────

RECENCY_DAYS = 7
CREATED_RECENCY_DAYS = 90
MAX_RESULTS_PER_QUERY = 100
MAX_PAGES = 10  # GitHub caps search results at 1000
WEEKLY_PAGES = 5  # weekly uses fewer pages to stay within rate limits

# Trending: now scrapes BOTH daily and weekly as primary sources
TRENDING_URLS = [
    ("daily", "https://github.com/trending?since=daily"),
    ("weekly", "https://github.com/trending?since=weekly"),
]

# Base query templates — {stars} placeholder filled at runtime
QUERY_TEMPLATES = [
    # Primary: high-signal repos
    {"q": "stars:>{stars}", "sort": "stars", "order": "desc", "star_base": 100},
    # Language expanders
    {"q": "language:python stars:>{stars}", "sort": "stars", "order": "desc", "star_base": 50},
    {"q": "language:typescript stars:>{stars}", "sort": "stars", "order": "desc", "star_base": 50},
    {"q": "language:go stars:>{stars}", "sort": "stars", "order": "desc", "star_base": 50},
    {"q": "language:rust stars:>{stars}", "sort": "stars", "order": "desc", "star_base": 50},
    # Topic targets (lower threshold by design)
    {"q": "topic:mcp stars:>{stars}", "sort": "stars", "order": "desc", "star_base": 10},
    {"q": "topic:agent-framework stars:>{stars}", "sort": "stars", "order": "desc", "star_base": 10},
    {"q": "topic:developer-tools stars:>{stars}", "sort": "stars", "order": "desc", "star_base": 20},
    {"q": "topic:hermes-plugin stars:>{stars}", "sort": "stars", "order": "desc", "star_base": 5},
    # v4.1: stack-specific queries
    {"q": "topic:react-native stars:>{stars}", "sort": "stars", "order": "desc", "star_base": 20},
    {"q": "topic:flutter stars:>{stars}", "sort": "stars", "order": "desc", "star_base": 20},
    {"q": "topic:voice-assistant stars:>{stars}", "sort": "stars", "order": "desc", "star_base": 10},
    {"q": "topic:sports-prediction stars:>{stars}", "sort": "stars", "order": "desc", "star_base": 5},
]

# ── Default thresholds ──────────────────────────────────────────────

DEFAULT_THRESHOLDS = {
    # v4.1: tuned down from 100 — broader net with strong noise filtering
    "star_threshold": 75,
    "min_star_threshold": 25,
    "max_star_threshold": 500,
    "noise_keywords": ["awesome", "curated list", "awesome list", "learn", "tutorial", "list", "resource", "cheatsheet"],
    "language_filters": ["HTML", "CSS", "Markdown"],
    # High-precision spam patterns for star-farmed repos (date-suffixed names,
    # cracked-software bait). Conservative: must not catch llama-3 / gpt-4.
    "spam_name_patterns": [
        r"-(19|20)\d\d(-|$)",
        r"(?i)(crack|keygen|nulled|allprompts|free-?download|activation-?key|license-?key|-latest-|version-\d)",
    ],
    "dead_repo_forks_ratio": 3.0,
    "dead_repo_min_stars": 10,
    "consecutive_noise_high_days": 3,
    "consecutive_signal_good_days": 3,
    "consecutive_signal_low_days": 5,
    "noise_high_threshold_pct": 40.0,
    "noise_low_threshold_pct": 20.0,
    "signal_high_threshold_pct": 60.0,
    "signal_low_threshold_pct": 10.0,
    "star_adjust_step": 25,
    "history": [],
    "last_tuned": None,
}


# ── Thresholds ──────────────────────────────────────────────────────

def load_thresholds():
    """Load thresholds.json, falling back to defaults with a fresh history entry."""
    if not os.path.exists(THRESHOLDS_FILE):
        save_thresholds(DEFAULT_THRESHOLDS)
        return dict(DEFAULT_THRESHOLDS)
    try:
        with open(THRESHOLDS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        # Merge with defaults so new keys propagate
        merged = dict(DEFAULT_THRESHOLDS)
        merged.update(data)
        return merged
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_THRESHOLDS)


def save_thresholds(thresholds):
    """Persist thresholds to disk."""
    try:
        atomic_write_json(THRESHOLDS_FILE, thresholds)
    except (OSError, ValueError, TypeError) as e:
        print(f"WARN: Failed to write thresholds: {e}", file=sys.stderr)


def tune_and_publish(noise_rate_pct, signal_rate_pct, acquisition_outcome=None):
    """Load, decide and publish thresholds as ONE transaction.

    The previous sequence loaded thresholds, tuned, then saved. Two
    overlapping runs could both load 100, one apply 100 -> 75, and the other
    publish its stale 100 decision — erasing the adjustment and one history
    event. Re-reading inside the lock means the decision is always made
    against the current settings.
    """
    with StateLock():
        current = load_thresholds()
        updated, actions = self_tune(
            current, noise_rate_pct, signal_rate_pct, acquisition_outcome
        )
        save_thresholds(updated)
        return updated, actions


def build_queries(thresholds):
    """Build query dicts with dynamic star thresholds applied.
    For primary/language queries: enforce global threshold as floor.
    For topic queries: keep intentionally low threshold (no floor).
    """
    base_threshold = thresholds["star_threshold"]
    queries = []
    for tpl in QUERY_TEMPLATES:
        q_template = tpl["q"]
        # Detect topic queries by presence of "topic:" in the template
        is_topic_query = "topic:" in q_template
        if is_topic_query:
            # Topic queries: use exactly the star_base from template (no floor)
            star_eff = tpl["star_base"]
        else:
            # Primary/language queries: enforce global threshold as minimum
            star_eff = max(tpl["star_base"], base_threshold)
        q = q_template.replace("{stars}", str(star_eff))
        queries.append({"q": q, "sort": tpl["sort"], "order": tpl["order"]})
    return queries


# ── Self-Tuning ─────────────────────────────────────────────────────

METRICS_LOOKBACK_DAYS = 7


def load_metrics():
    """Load metrics history from disk."""
    if not os.path.exists(METRICS_FILE):
        return []
    try:
        with open(METRICS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def append_metrics_entry(entry):
    """Append a metrics entry, prune to 365 max."""
    # Load -> append -> publish must be one critical section, or two overlapping
    # runs each read the same history and one run's entry is lost.
    try:
        with StateLock():
            metrics = load_metrics()
            metrics.append(entry)
            # Prune to 365 entries
            if len(metrics) > 365:
                metrics = metrics[-365:]
            atomic_write_json(METRICS_FILE, metrics)
    except (OSError, ValueError, TypeError) as e:
        print(f"WARN: Failed to write metrics: {e}", file=sys.stderr)


def _count_consecutive(window, thresholds):
    """Analyze metrics window for consecutive days exceeding thresholds.
    Returns (high_noise_days, good_signal_days, low_signal_days, low_noise_days).
    """
    hnoise = gsignal = lsig = lnoise = 0
    for entry in window:
        nr = entry.get("noise_rate_pct", 0)
        sr = entry.get("signal_rate_pct", 0)

        hnoise = hnoise + 1 if nr >= thresholds["noise_high_threshold_pct"] else 0
        gsignal = gsignal + 1 if (sr >= thresholds["signal_high_threshold_pct"] and nr <= thresholds["noise_low_threshold_pct"]) else 0
        lsig = lsig + 1 if sr <= thresholds["signal_low_threshold_pct"] else 0
        lnoise = lnoise + 1 if nr <= thresholds["noise_low_threshold_pct"] else 0

    return hnoise, gsignal, lsig, lnoise


def _adjust_threshold(thresholds, direction, step_multiplier=1):
    """Adjust star_threshold in the given direction, return (old, new, changed)."""
    old = thresholds["star_threshold"]
    if direction == "tighten":
        new = min(old + thresholds["star_adjust_step"] * step_multiplier, thresholds["max_star_threshold"])
    elif direction == "ease":
        new = max(old - thresholds["star_adjust_step"], thresholds["min_star_threshold"])
    else:
        return old, old, False
    if new != old:
        thresholds["star_threshold"] = new
        return old, new, True
    return old, old, False


def self_tune(thresholds, noise_rate_pct, signal_rate_pct, acquisition_outcome=None):
    """
    Read recent metrics and adjust thresholds based on signal quality.
    Returns (updated_thresholds, tuning_actions_log) where tuning_actions_log
    is a list of human-readable strings describing what changed.
    """
    # Eligibility: a run whose acquisition was not clean is not an
    # observation. An outage produces 0% signal, which used to be fed to the
    # controller as if the query policy had genuinely failed.
    if acquisition_outcome not in (None, ACQ_OK):
        return thresholds, [
            f"SKIP: acquisition {acquisition_outcome} — not tuning from an "
            f"incomplete scan (star_threshold stays at {thresholds['star_threshold']})"
        ]

    actions = []
    metrics = load_metrics()

    # Build analysis window: last N entries + today
    recent = metrics[-METRICS_LOOKBACK_DAYS:] if len(metrics) >= METRICS_LOOKBACK_DAYS else metrics
    today = {"noise_rate_pct": noise_rate_pct, "signal_rate_pct": signal_rate_pct}
    window = list(recent) + [today] if recent else [today]

    if len(window) < 2:
        return thresholds, ["Not enough data to tune (need 2+ runs)"]

    hnoise, gsignal, lsig, lnoise = _count_consecutive(window, thresholds)
    star_changed = False

    # Rule 1: Sustained high noise → tighten
    if hnoise >= thresholds["consecutive_noise_high_days"]:
        old, new, changed = _adjust_threshold(thresholds, "tighten")
        if changed:
            star_changed = True
            actions.append(f"TIGHTEN: noise >{thresholds['noise_high_threshold_pct']}% for {hnoise}d — star_threshold {old} → {new}")

    # Rule 2: Sustained good signal + low noise → ease
    elif gsignal >= thresholds["consecutive_signal_good_days"] and lnoise >= thresholds["consecutive_signal_good_days"]:
        old, new, changed = _adjust_threshold(thresholds, "ease")
        if changed:
            star_changed = True
            actions.append(f"EASE: signal >{thresholds['signal_high_threshold_pct']}%, noise <{thresholds['noise_low_threshold_pct']}% for {gsignal}d — star_threshold {old} → {new}")

    # Rule 3: Sustained very low signal → aggressive tighten
    elif lsig >= thresholds["consecutive_signal_low_days"]:
        old, new, changed = _adjust_threshold(thresholds, "tighten", 2)
        if changed:
            star_changed = True
            actions.append(f"AGGRESSIVE TIGHTEN: signal <{thresholds['signal_low_threshold_pct']}% for {lsig}d — star_threshold {old} → {new}")

    if not star_changed:
        actions.append(f"HOLD: noise {noise_rate_pct:.1f}%, signal {signal_rate_pct:.1f}% — thresholds unchanged")

    # Record tuning event
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    thresholds.setdefault("history", []).append({
        "tuned_at": now,
        "noise_rate_pct": noise_rate_pct,
        "signal_rate_pct": signal_rate_pct,
        "star_threshold": thresholds["star_threshold"],
        "actions": list(actions),
    })
    if len(thresholds["history"]) > 90:
        thresholds["history"] = thresholds["history"][-90:]
    thresholds["last_tuned"] = now

    return thresholds, actions


# ── GitHub API ──────────────────────────────────────────────────────

SEARCH_RATE_LIMIT = 30  # GitHub Search API: 30 requests per minute
_search_call_timestamps = []


def wait_for_rate_limit():
    """Ensure we don't exceed GitHub Search API rate limit (30 req/min).
    Tracks timestamps of recent calls; sleeps if the limit is about to be hit.
    """
    global _search_call_timestamps
    now = time.time()
    # Prune timestamps older than 60s
    _search_call_timestamps = [t for t in _search_call_timestamps if now - t < 60]
    if len(_search_call_timestamps) >= SEARCH_RATE_LIMIT:
        # Need to wait until oldest timestamp expires (60s after it was recorded)
        wait = _search_call_timestamps[0] + 60 - now
        if wait > 0:
            time.sleep(wait + 0.5)  # add buffer
        # Prune again after waiting
        _search_call_timestamps = [t for t in _search_call_timestamps if time.time() - t < 60]
    _search_call_timestamps.append(time.time())


def get_date_filters():
    """Return (pushed_cutoff, created_cutoff) for compound recency filter."""
    pushed_cutoff = datetime.now(timezone.utc) - timedelta(days=RECENCY_DAYS)
    created_cutoff = datetime.now(timezone.utc) - timedelta(days=CREATED_RECENCY_DAYS)
    return pushed_cutoff.strftime("%Y-%m-%d"), created_cutoff.strftime("%Y-%m-%d")


def gh_auth_token():
    """Get GitHub PAT from env or gh CLI. Result is cached after first call."""
    env_token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    if env_token:
        return env_token

    if not hasattr(gh_auth_token, "_token"):
        try:
            result = subprocess.run(
                ["gh", "auth", "token"], capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                print(f"WARN: gh auth failed: {result.stderr.strip()}", file=sys.stderr)
                gh_auth_token._token = None
            else:
                gh_auth_token._token = result.stdout.strip()
        except FileNotFoundError:
            print("WARN: gh CLI not found and GITHUB_TOKEN not set. Running unauthenticated (60 req/hr).", file=sys.stderr)
            gh_auth_token._token = None
        except Exception as e:
            print(f"WARN: gh auth exception: {e}", file=sys.stderr)
            gh_auth_token._token = None
    return gh_auth_token._token


# Acquisition outcomes. Every failure used to collapse to ([], 0), which is
# indistinguishable from a genuinely empty search: an outage looked like a
# clean scan and self_tune consumed it as a real observation.
ACQ_OK = "ok"
ACQ_NO_AUTH = "unauthenticated"
ACQ_FORBIDDEN = "permission_denied"
ACQ_RATE_LIMITED = "rate_limited"
ACQ_TIMEOUT = "timeout"
ACQ_ERROR = "error"

_ACQ_SEVERITY = {
    ACQ_OK: 0,
    ACQ_RATE_LIMITED: 1,
    ACQ_TIMEOUT: 2,
    ACQ_ERROR: 3,
    ACQ_FORBIDDEN: 4,
    ACQ_NO_AUTH: 5,
}

_acquisition = {"calls": 0, "counts": {}, "detail": ""}


def reset_acquisition():
    """Start a fresh per-run acquisition record."""
    _acquisition["calls"] = 0
    _acquisition["counts"] = {}
    _acquisition["detail"] = ""


def record_acquisition(outcome, detail=""):
    """Record one request/source/page outcome for this run."""
    _acquisition["calls"] += 1
    _acquisition["counts"][outcome] = _acquisition["counts"].get(outcome, 0) + 1
    if detail:
        _acquisition["detail"] = detail


def acquisition_status():
    """Aggregate outcome for the whole run; the worst class seen governs."""
    counts = dict(_acquisition["counts"])
    worst = ACQ_OK
    for outcome in counts:
        if _ACQ_SEVERITY.get(outcome, 3) > _ACQ_SEVERITY.get(worst, 0):
            worst = outcome
    return {
        "calls": _acquisition["calls"],
        "counts": counts,
        "worst": worst,
        "partial": any(o != ACQ_OK for o in counts),
        "detail": _acquisition["detail"],
    }


def github_search(query, sort="stars", order="desc", per_page=100, page=1):
    """Call GitHub Search API. Returns (items, total_count).
    Returns ([], -1) on rate-limit (HTTP 403/429) so callers can bail.
    """
    token = gh_auth_token()
    if not token:
        record_acquisition(ACQ_NO_AUTH, "no GitHub token")
        return [], 0

    pushed_q, created_q = get_date_filters()
    full_q = f"{query} pushed:>{pushed_q} created:>{created_q}"
    params = urllib.parse.urlencode({
        "q": full_q, "sort": sort, "order": order,
        "per_page": per_page, "page": page
    })
    url = f"https://api.github.com/search/repositories?{params}"

    wait_for_rate_limit()  # throttle to 30 req/min
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "GitRadar/1.0")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            items = data.get("items", [])
            record_acquisition(ACQ_OK)
            return items, data.get("total_count", 0)
    except urllib.error.HTTPError as e:
        print(f"WARN: GitHub API error {e.code} for query '{query[:60]}': {e.reason}", file=sys.stderr)
        if e.code == 429:
            record_acquisition(ACQ_RATE_LIMITED, f"HTTP {e.code}")
        elif e.code == 403:
            record_acquisition(ACQ_FORBIDDEN, f"HTTP {e.code}")
        else:
            record_acquisition(ACQ_ERROR, f"HTTP {e.code}")
        if e.code in (403, 429):
            return [], -1  # rate-limited sentinel
        return [], 0
    except (TimeoutError, urllib.error.URLError) as e:
        record_acquisition(ACQ_TIMEOUT, str(e))
        print(f"WARN: GitHub API timeout: {e}", file=sys.stderr)
        return [], 0
    except Exception as e:
        record_acquisition(ACQ_ERROR, str(e))
        print(f"WARN: GitHub API exception: {e}", file=sys.stderr)
        return [], 0


# ── GitHub Trending (PRIMARY SOURCE) ────────────────────────────────


def scrape_trending(url, label):
    """Scrape one trending page. Returns list of dicts with full_name, source."""
    results = []
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "GitRadar/1.0",
            "Accept": "text/html",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        # Extract owner/name from all repo cards
        for match in re.finditer(
            r'<h2[^>]*class="[^"]*h3[^"]*"[^>]*>.*?<a[^>]*href="/([^/"]+)/([^/"]+)"',
            html, re.DOTALL
        ):
            owner = match.group(1)
            repo = match.group(2)
            if owner and repo:
                results.append({
                    "full_name": f"{owner}/{repo}",
                    "source": f"trending-{label}",
                })
    except Exception as e:
        print(f"WARN: Trending scrape failed ({label}): {e}", file=sys.stderr)
    return results


def scrape_all_trending():
    """Scrape all trending pages (daily + weekly). Returns deduped list."""
    seen = set()
    all_results = []
    for label, url in TRENDING_URLS:
        repos = scrape_trending(url, label)
        for r in repos:
            if r["full_name"] not in seen:
                seen.add(r["full_name"])
                all_results.append(r)
    print(f"TRENDING: {len(all_results)} unique repos from trending pages", file=sys.stderr)
    return all_results


def enrich_trending_repos(trending_repos):
    """Fetch real metadata from GitHub API for trending repos.
    
    Trending scraping only gets repo names, resulting in 0 stars/metadata,
    which causes all trending repos to be filtered as 'dead_repo'.
    This function fetches actual metadata so trending repos can be scored properly.
    """
    if not trending_repos:
        return []
    
    enriched = []
    for repo in trending_repos:
        full_name = repo["full_name"]
        url = f"https://api.github.com/repos/{full_name}"
        
        token = gh_auth_token()
        if token:
            req = urllib.request.Request(url)
            req.add_header("Authorization", f"Bearer {token}")
            req.add_header("Accept", "application/vnd.github+json")
        else:
            req = urllib.request.Request(url)
            req.add_header("Accept", "application/vnd.github+json")
        
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode())
                # Merge scraped trending data with real API data
                enriched_repo = {
                    "full_name": data.get("full_name", full_name),
                    "description": data.get("description", ""),
                    "stars": data.get("stargazers_count", 0),
                    "forks": data.get("forks_count", 0),
                    "language": data.get("language", ""),
                    "topics": data.get("topics", []),
                    "created_at": data.get("created_at", ""),
                    "pushed_at": data.get("pushed_at", ""),
                    "open_issues": data.get("open_issues_count", 0),
                    "license": (data.get("license") or {}).get("spdx_id", "") if data.get("license") else "",
                    "html_url": data.get("html_url", f"https://github.com/{full_name}"),
                    "source": "trending",
                }
                enriched.append(enriched_repo)
        except Exception as e:
            print(f"WARN: Failed to enrich trending repo {full_name}: {e}", file=sys.stderr)
            # Keep original scraped data as fallback
            enriched.append(repo)
    
    print(f"ENRICHED: {len(enriched)} trending repos with API metadata", file=sys.stderr)
    return enriched


# ── Repo Data ───────────────────────────────────────────────────────


def parse_star_count(item):
    """Safely parse star count from GitHub item (API key or our normalised key)."""
    val = item.get("stargazers_count") or item.get("stars") or 0
    return int(val)


def extract_repo(item):
    """Normalise a GitHub API item into our standard dict."""
    return {
        "full_name": item.get("full_name", ""),
        "description": (item.get("description") or "").strip(),
        "stars": parse_star_count(item),
        "forks": item.get("forks_count", 0) or 0,
        "language": item.get("language") or "",
        "topics": item.get("topics", []) or [],
        "created_at": item.get("created_at", ""),
        "pushed_at": item.get("pushed_at", ""),
        "open_issues": item.get("open_issues_count", 0) or 0,
        "license": item.get("license", {}).get("spdx_id", "") if item.get("license") else "",
        "html_url": item.get("html_url", ""),
        "source": "api",
    }


# ── Pre-Filtering ──────────────────────────────────────────────────


def build_noise_patterns(thresholds):
    """Build NOISE_PATTERNS dict from tuned thresholds."""
    keywords = thresholds.get("noise_keywords", DEFAULT_THRESHOLDS["noise_keywords"])
    lang_filters = thresholds.get("language_filters", DEFAULT_THRESHOLDS["language_filters"])
    fork_ratio = thresholds.get("dead_repo_forks_ratio", 3.0)
    dead_min = thresholds.get("dead_repo_min_stars", 10)
    spam_patterns = thresholds.get("spam_name_patterns", DEFAULT_THRESHOLDS["spam_name_patterns"])

    return {
        "awesome_list": lambda r: any(
            kw in (r.get("description", "") + " " + " ".join(r.get("topics", []))).lower()
            for kw in ["awesome", "curated list", "awesome list"]
        ),
        "tutorial_content": lambda r: any(
            r["full_name"].lower().startswith(prefix)
            for prefix in ["learn-", "awesome-", "tutorial-"]
        ),
        # Only a measured star count can prove a repo is dead. Rows whose
        # metadata could not be fetched are unknown, not empty: filtering
        # them here was what deleted every Trending repo.
        "dead_repo": lambda r: metadata_is_known(r) and (
            parse_star_count(r) < dead_min
            or (r.get("forks", 0) or 0) > parse_star_count(r) * fork_ratio
        ),
        "non_code": lambda r: r.get("language", "") in lang_filters,
        "name_noise": lambda r: any(
            kw in r["full_name"].lower().split("/")[1]
            for kw in keywords
        ),
        "spam_name": lambda r: any(
            re.search(p, r["full_name"].split("/")[-1])
            for p in spam_patterns
        ),
    }


METADATA_UNKNOWN_STATES = ("unknown", "unavailable", "deferred")


def metadata_is_known(repo):
    """True only when the star count is a real measurement, not a placeholder."""
    if repo.get("metadata_status") in METADATA_UNKNOWN_STATES:
        return False
    return repo.get("stars") is not None


NOISE_ORDER = ["awesome_list", "non_code", "name_noise", "spam_name", "tutorial_content", "dead_repo"]


def classify_noise(repo, thresholds):
    """Returns (is_noise: bool, reason: str). Uses tuned thresholds."""
    patterns = build_noise_patterns(thresholds)
    for rule in NOISE_ORDER:
        if patterns[rule](repo):
            return True, rule
    return False, ""


def deduplicate(repos):
    """Deduplicate by full_name, keeping highest-star entry."""
    seen = {}
    for r in repos:
        name = r["full_name"]
        if name not in seen or parse_star_count(r) > parse_star_count(seen[name]):
            seen[name] = r
    return list(seen.values())


# ── Cache v2 (pushed_at tracking) ────────────────────────────────────

CACHE_TTL_DAYS = 14  # repos expire from cache after 14 days, allowing re-discovery


def load_cache_v2():
    """
    Load cache with pushed_at tracking.
    Returns (active_set: set of repo names, pushed_map: {name: pushed_at}).

    Supports both old format {seen: {repo: date_str}} and v2 format
    {seen: {repo: {first_seen, last_pushed, last_checked}}}.
    """
    if not os.path.exists(CACHE_FILE):
        return set(), {}
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        seen_data = data.get("seen", {})
        if isinstance(seen_data, list):
            # Old format: list of names — clear and migrate to v2
            print(f"CACHE: old list format ({len(seen_data)} entries), migrating to v2", file=sys.stderr)
            return set(), {}
        if isinstance(seen_data, dict):
            now = datetime.now(timezone.utc)
            active = set()
            pushed_map = {}
            expired = 0
            for repo, entry in seen_data.items():
                # Support both new dict format and old string format
                if isinstance(entry, str):
                    date_str = entry
                    pushed = ""
                elif isinstance(entry, dict):
                    date_str = entry.get("first_seen", "")
                    pushed = entry.get("last_pushed", "")
                else:
                    date_str = ""
                    pushed = ""
                try:
                    seen_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                    if (now - seen_date).days < CACHE_TTL_DAYS:
                        active.add(repo)
                        if pushed:
                            pushed_map[repo] = pushed
                    else:
                        expired += 1
                except (ValueError, TypeError):
                    expired += 1
            if expired > 0:
                print(f"CACHE: pruned {expired} expired entries (TTL={CACHE_TTL_DAYS}d), {len(active)} active", file=sys.stderr)
            return active, pushed_map
    except (json.JSONDecodeError, KeyError):
        return set(), {}
    return set(), {}


def cache_entry_is_active(entry, now=None):
    """True when a stored cache entry is still inside the TTL window."""
    now = now or datetime.now(timezone.utc)
    first_seen = entry if isinstance(entry, str) else (
        entry.get("first_seen", "") if isinstance(entry, dict) else ""
    )
    try:
        seen_date = datetime.fromisoformat(first_seen.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    return (now - seen_date).days < CACHE_TTL_DAYS


def save_cache_v2(seen_set, pushed_map=None, checked_set=None):
    """Persist the cache, merging against the live store under the lock.

    Rewriting only the caller's snapshot deleted names another run added
    while this one was collecting, so the merge happens against the store as
    it stands now. Expired entries are dropped, keeping the merge bounded by
    the documented TTL rather than an unbounded union.

    last_checked advances only for repos this run actually returned; a
    tracked entry nobody verified keeps its previous timestamp.
    """
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with StateLock():
            now_dt = datetime.now(timezone.utc)
            now_str = now_dt.isoformat().replace("+00:00", "Z")

            stored = {}
            if os.path.exists(CACHE_FILE):
                try:
                    with open(CACHE_FILE, encoding="utf-8") as f:
                        data = json.load(f)
                    seen_data = data.get("seen", {})
                    if isinstance(seen_data, dict):
                        stored = seen_data
                except (json.JSONDecodeError, KeyError):
                    stored = {}

            new_entries = {}
            for repo, entry in stored.items():
                if cache_entry_is_active(entry, now_dt):
                    new_entries[repo] = dict(entry) if isinstance(entry, dict) else {
                        "first_seen": entry, "last_pushed": "", "last_checked": ""
                    }

            preserved = 0
            new_count = 0
            for repo in seen_set:
                if repo in new_entries:
                    entry = dict(new_entries[repo])
                    incoming = (pushed_map or {}).get(repo)
                    current = entry.get("last_pushed", "")
                    if incoming and (not current or incoming > current):
                        entry["last_pushed"] = incoming
                    if checked_set is None or repo in checked_set:
                        entry["last_checked"] = now_str
                    new_entries[repo] = entry
                    preserved += 1
                else:
                    was_checked = checked_set is None or repo in checked_set
                    new_entries[repo] = {
                        "first_seen": now_str,
                        "last_pushed": (pushed_map or {}).get(repo, ""),
                        "last_checked": now_str if was_checked else "",
                    }
                    new_count += 1

            if new_count > 0 or preserved > 0:
                print(f"CACHE: {preserved} preserved, {new_count} new entries", file=sys.stderr)
            atomic_write_json(CACHE_FILE, {"seen": new_entries})
    except Exception as e:
        print(f"WARN: Failed to write cache: {e}", file=sys.stderr)


# ── Bounded per-identity rechecks ────────────────────────────────────
#
# A repo discovery no longer returns has still not been checked. The
# per-run cap is an operator/API-budget decision, so this stays disabled
# with a truthful reason until that budget is configured; no quota is
# invented here.

RECHECK_BUDGET_ENV = "GITHUB_RADAR_RECHECK_BUDGET"
RECHECK_QUEUE_FILE = os.path.join(DATA_DIR, "recheck-queue.json")


def recheck_budget(explicit=None):
    """Operator-supplied per-run recheck cap, or None when unset/invalid."""
    if explicit is not None:
        try:
            return int(explicit) if int(explicit) > 0 else None
        except (TypeError, ValueError):
            return None
    raw = (os.environ.get(RECHECK_BUDGET_ENV) or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def load_recheck_queue():
    if not os.path.exists(RECHECK_QUEUE_FILE):
        return {"pending": [], "checked_through": None}
    try:
        with open(RECHECK_QUEUE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"pending": [], "checked_through": None}
        return {
            "pending": [str(x) for x in data.get("pending", [])],
            "checked_through": data.get("checked_through"),
        }
    except (json.JSONDecodeError, OSError):
        return {"pending": [], "checked_through": None}


def save_recheck_queue(queue):
    with StateLock():
        atomic_write_json(RECHECK_QUEUE_FILE, {
            "pending": list(queue.get("pending", [])),
            "checked_through": queue.get("checked_through"),
        })


def plan_rechecks(tracked_ids, budget, queue=None):
    """Pick at most `budget` identities, keeping a durable backlog."""
    queue = queue if queue is not None else load_recheck_queue()
    pending = list(queue.get("pending", []))
    seen = set(pending)
    for ident in tracked_ids:
        if ident not in seen:
            pending.append(ident)
            seen.add(ident)
    return pending[:budget], pending[budget:]


def run_rechecks(identities, fetch=None, budget=None):
    """Recheck a bounded batch; counters separate queried from not-queried."""
    fetch = fetch or _repo_metadata
    counters = {
        "attempted": 0, "checked": 0, "changed": 0, "unchanged": 0,
        "unavailable": 0, "failed": 0, "deferred": 0,
    }
    _, pushed_map = load_cache_v2()
    for ident in identities:
        counters["attempted"] += 1
        try:
            data = fetch(ident)
        except Exception:
            counters["failed"] += 1
            record_acquisition(ACQ_ERROR, f"recheck failed for {ident}")
            continue
        if not data:
            counters["unavailable"] += 1
            record_acquisition(ACQ_ERROR, f"recheck unavailable for {ident}")
            continue
        counters["checked"] += 1
        new_pushed = data.get("pushed_at", "")
        old_pushed = pushed_map.get(ident, "")
        if new_pushed and old_pushed and new_pushed > old_pushed:
            counters["changed"] += 1
        else:
            counters["unchanged"] += 1
    counters["budget"] = budget
    return counters


def recheck_tracked(budget=None, fetch=None):
    """Operator-gated entry point for identity-based rechecks."""
    resolved = recheck_budget(budget)
    if not resolved:
        return {
            "enabled": False,
            "reason": (
                f"no per-run recheck budget configured "
                f"(set {RECHECK_BUDGET_ENV}); identities were not rechecked"
            ),
            "attempted": 0, "checked": 0, "deferred": 0, "not_queried": 0,
        }
    queue = load_recheck_queue()
    active_cache, _ = load_cache_v2()
    batch, remaining = plan_rechecks(sorted(active_cache), resolved, queue)
    counters = run_rechecks(batch, fetch=fetch, budget=resolved)
    counters["not_queried"] = len(remaining)
    counters["deferred"] = len(remaining)
    counters["enabled"] = True
    save_recheck_queue({"pending": remaining, "checked_through": batch[-1] if batch else None})
    return counters


def _repo_metadata(full_name, timeout=10):
    """Fetch one repo's metadata; None when unavailable."""
    token = gh_auth_token()
    url = f"https://api.github.com/repos/{full_name}"
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "GitRadar/1.0")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


# ── Outcome recording (minimal, local) ───────────────────────────────

OUTCOMES_FILE = os.path.join(DATA_DIR, "outcomes.json")
OUTCOME_STATES = ("irrelevant", "already-known", "investigate", "tried", "useful")

METRIC_LABEL_NOTE = (
    "filter_accepted counts findings that passed filtering; it is not a "
    "measure of usefulness or adoption"
)


def finding_identity(repo_or_name):
    """Stable identity for a finding, independent of run or casing."""
    name = repo_or_name if isinstance(repo_or_name, str) else repo_or_name.get("full_name", "")
    return str(name).strip().lower()


def load_outcomes():
    if not os.path.exists(OUTCOMES_FILE):
        return {"schema": 1, "events": {}}
    try:
        with open(OUTCOMES_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "events" not in data:
            return {"schema": 1, "events": {}}
        return data
    except (json.JSONDecodeError, OSError):
        return {"schema": 1, "events": {}}


def record_outcome(finding, state, note=None, source="local"):
    """Record one outcome per (finding, state), deduplicated."""
    if state not in OUTCOME_STATES:
        raise ValueError(f"unknown outcome state {state!r}; expected one of {OUTCOME_STATES}")
    ident = finding_identity(finding)
    if not ident:
        raise ValueError("finding identity must be non-empty")
    key = f"{ident}|{state}"
    with StateLock():
        data = load_outcomes()
        events = data.setdefault("events", {})
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        event = events.get(key)
        if event is None:
            events[key] = {
                "finding": ident, "state": state, "count": 1,
                "first_at": now, "last_at": now, "source": source,
                "note": note or "",
            }
        else:
            event["count"] = int(event.get("count", 1)) + 1
            event["last_at"] = now
            if note:
                event["note"] = note
        atomic_write_json(OUTCOMES_FILE, data)
        return dict(events[key])


def read_outcomes(ident=None, state=None):
    data = load_outcomes()
    rows = list(data.get("events", {}).values())
    if ident is not None:
        key = finding_identity(ident)
        rows = [r for r in rows if r.get("finding") == key]
    if state is not None:
        rows = [r for r in rows if r.get("state") == state]
    return sorted(rows, key=lambda r: r.get("last_at", ""))


def outcome_summary():
    data = load_outcomes()
    counts = {state: 0 for state in OUTCOME_STATES}
    for event in data.get("events", {}).values():
        state = event.get("state")
        if state in counts:
            counts[state] += int(event.get("count", 0))
    counts["no_outcome_recorded"] = None
    return counts


def score_repo(repo):
    """Compute a 0-100 relevance score for a repo. Higher is more interesting."""
    stars = parse_star_count(repo)
    # Log-scaled star score: 0 stars → 20, 2000 stars → 100
    star_score = min(100.0, 20 + 80 * math.log10(stars + 1) / math.log10(2000))

    # Generic keyword relevance
    text_blob = (
        f"{repo.get('description', '')} "
        f"{' '.join(repo.get('topics', []))} "
        f"{repo.get('language', '')}"
    ).lower()
    relevance_hits = sum(1 for kw in RELEVANCE_KEYWORDS if kw in text_blob)
    relevance_score = min(100, relevance_hits * RELEVANCE_WEIGHT)

    # Freshness: decays 10 pts per day since last push
    try:
        pushed = datetime.fromisoformat(repo.get("pushed_at", "").replace("Z", "+00:00"))
        days_since_push = (datetime.now(timezone.utc) - pushed).days
        freshness_score = max(0, 100 - days_since_push * 10)
    except (ValueError, TypeError):
        freshness_score = 50

    # Activity: how recent the repo was created
    try:
        created = datetime.fromisoformat(repo.get("created_at", "").replace("Z", "+00:00"))
        age_days = (datetime.now(timezone.utc) - created).days
        active_score = 100 if age_days < 30 else (80 if age_days < 90 else 50)
    except (ValueError, TypeError):
        active_score = 50

    score = round(
        star_score * 0.4
        + relevance_score * 0.3
        + freshness_score * 0.2
        + active_score * 0.1,
        1,
    )
    return score


# ── Scoring ─────────────────────────────────────────────────────────
#
# README: "Define your technology stack for scoring in config/stack.json".
# This script is the one that actually writes discoveries.json, but it used
# its own hardcoded keyword scorer and never consulted stack.json — so the
# configured preferences had no effect on the published output, and the
# documented `classification` field was emitted by nothing at all
# (gitradar-score.py computes it but is not on this path).
#
# Load the config-driven scorer from the sibling script so there is exactly
# one scoring implementation, and emit the documented fields from here.

_SCORE_MODULE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gitradar-score.py")
_score_module = None


def _load_score_module():
    """Import the sibling scoring script; None when unavailable."""
    global _score_module
    if _score_module is not None:
        return _score_module
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("gitradar_score", _SCORE_MODULE_PATH)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _score_module = module
        return module
    except Exception as e:
        print(f"WARN: config-driven scorer unavailable ({e}); using fallback", file=sys.stderr)
        return None


CLASSIFICATION_REASONS = {
    "ADOPT": "High-score repo: evaluate for adoption before installing.",
    "EXTRACT": "Strong match: worth extracting concepts or patterns.",
    "FORK/PRODUCT": "Could become a standalone product or fork.",
    "PLUGIN/SKILL": "Useful tooling or extension point worth trialling.",
    "INSPIRATION": "Moderate signal: file for future reference.",
    "NOISE": "Low signal against the configured stack.",
}


def classify_score(score):
    """Fallback banding, used only when the scoring script cannot be loaded."""
    for floor, label in ((80, "ADOPT"), (60, "EXTRACT"), (50, "FORK/PRODUCT"),
                         (40, "PLUGIN/SKILL"), (20, "INSPIRATION"), (0, "NOISE")):
        if score >= floor:
            return label
    return "NOISE"


def score_all(repos):
    """Score, classify and explain every repo, then sort by score descending.

    Uses the documented config/stack.json preferences through
    gitradar-score.py so the configured stack actually shapes the output.
    The rationale is deterministic — it states the score and label — and is
    kept separate from any optional prose so rewriting wording cannot erase
    why a label was assigned.
    """
    module = _load_score_module()
    stack = None
    if module is not None:
        try:
            stack = module.load_stack()
        except Exception as e:
            print(f"WARN: could not load stack preferences ({e})", file=sys.stderr)
            module = None

    scored = []
    for r in repos:
        if module is not None:
            score = round(module.compute_score(r, stack), 2)
            label = module.classify_repo(score)
        else:
            score = score_repo(r)
            label = classify_score(score)
        r["score"] = score
        r["classification"] = label
        r["why"] = f"{CLASSIFICATION_REASONS.get(label, '')} (score {score}, {label})"
        r["score_source"] = "config/stack.json" if module is not None else "builtin-keywords"
        scored.append(r)

    if module is not None:
        print(f"SCORE: using config/stack.json via gitradar-score.py", file=sys.stderr)
    else:
        print("SCORE: config-driven scorer unavailable; builtin keywords only", file=sys.stderr)
    return sorted(scored, key=lambda x: x.get("score", 0), reverse=True)


# ── Collection ──────────────────────────────────────────────────────


def collect_daily(queries):
    """
    Daily mode: dedup against cache. Trending is a primary source.
    Returns (repos, extra_stats) where extra_stats describes the collection.
    """
    active_cache, pushed_map = load_cache_v2()
    seen = set(active_cache)
    discovered = set()  # repos this run actually returned
    all_repos = []
    rate_limited = False

    # Phase 1: GitHub Search API (dedup'd against cache)
    for query_def in queries:
        if rate_limited:
            break
        q = query_def["q"]
        sort = query_def.get("sort", "stars")
        order = query_def.get("order", "desc")

        page = 1
        while page <= MAX_PAGES and not rate_limited:
            items, total = github_search(q, sort, order, 100, page)
            if total == -1:  # rate-limited sentinel
                rate_limited = True
                break
            if not items:
                break
            for item in items:
                full_name = item.get("full_name", "")
                if full_name and full_name not in discovered:
                    discovered.add(full_name)
                    seen.add(full_name)
                    all_repos.append(extract_repo(item))
            page += 1

    # Phase 2: Trending (PRIMARY source — scrape daily AND weekly)
    trending = scrape_all_trending()
    trending = enrich_trending_repos(trending)
    trending_new = 0
    for t in trending:
        name = t["full_name"]
        if name not in discovered:
            discovered.add(name)
            seen.add(name)
            # Keep what enrichment actually returned. Rebuilding the row
            # with stars=0 threw the fetched metadata away and made every
            # Trending repo a dead_repo, so this "primary source" contributed
            # nothing at all.
            all_repos.append({
                "full_name": name,
                "description": t.get("description", ""),
                "stars": t.get("stars"),
                "forks": t.get("forks"),
                "language": t.get("language", ""),
                "topics": list(t.get("topics") or []),
                "created_at": t.get("created_at", ""),
                "pushed_at": t.get("pushed_at", ""),
                "open_issues": t.get("open_issues"),
                "license": t.get("license", ""),
                "html_url": t.get("html_url") or f"https://github.com/{name}",
                "source": t.get("source", "trending"),
                "metadata_status": t.get("metadata_status", "unknown"),
            })
            trending_new += 1

    # Build pushed_map from collected repos (update last_pushed in cache)
    collected_pushed = {}
    for r in all_repos:
        if r.get("pushed_at"):
            collected_pushed[r["full_name"]] = r["pushed_at"]

    save_cache_v2(seen, collected_pushed)

    api_new = len(all_repos) - trending_new
    extra_stats = {
        "total": len(seen),
        "new_api": api_new,
        "new_trending": trending_new,
        "cached_active": len(active_cache),
    }
    print(
        f"DAILY: {len(all_repos)} new ({api_new} API, {trending_new} trending), "
        f"{len(active_cache)} cached active",
        file=sys.stderr,
    )
    return all_repos, extra_stats


def collect_weekly(queries):
    """
    Weekly mode: full re-scan without cache dedup.
    Uses fewer pages (WEEKLY_PAGES=5) to stay within GitHub's 30 req/min search API limit.
    Compares pushed_at against cached last_pushed to find repos with new activity.
    Returns (repos, extra_stats).
    """
    active_cache, pushed_map = load_cache_v2()
    all_repos = []
    # `seen` is written back, so it must start from the tracked cache: it
    # previously began empty, which replaced the whole 14-day watchlist with
    # whatever one scan returned.
    seen = set(active_cache)
    discovered = set()  # repos this run actually returned
    rate_limited = False

    # Phase 1: Full API scan — find ALL matching repos, including cached ones
    for query_def in queries:
        if rate_limited:
            break
        q = query_def["q"]
        sort = query_def.get("sort", "stars")
        order = query_def.get("order", "desc")

        page = 1
        while page <= WEEKLY_PAGES and not rate_limited:
            items, total = github_search(q, sort, order, 100, page)
            if total == -1:
                rate_limited = True
                break
            if not items:
                break
            for item in items:
                full_name = item.get("full_name", "")
                if full_name and full_name not in discovered:
                    discovered.add(full_name)
                    seen.add(full_name)
                    all_repos.append(extract_repo(item))
            page += 1

    # Phase 2: Trending (weekly mode)
    trending = scrape_all_trending()
    trending = enrich_trending_repos(trending)
    for t in trending:
        name = t["full_name"]
        if name not in discovered:
            discovered.add(name)
            seen.add(name)
            # Keep what enrichment actually returned. Rebuilding the row
            # with stars=0 threw the fetched metadata away and made every
            # Trending repo a dead_repo, so this "primary source" contributed
            # nothing at all.
            all_repos.append({
                "full_name": name,
                "description": t.get("description", ""),
                "stars": t.get("stars"),
                "forks": t.get("forks"),
                "language": t.get("language", ""),
                "topics": list(t.get("topics") or []),
                "created_at": t.get("created_at", ""),
                "pushed_at": t.get("pushed_at", ""),
                "open_issues": t.get("open_issues"),
                "license": t.get("license", ""),
                "html_url": t.get("html_url") or f"https://github.com/{name}",
                "source": t.get("source", "trending"),
                "metadata_status": t.get("metadata_status", "unknown"),
            })

    # Phase 3: Compare against cached pushed_at
    checked = discovered & active_cache          # tracked AND seen again
    unavailable = active_cache - discovered      # tracked, not returned
    not_queried = set()                          # no bounded recheck queue yet
    re_evaluated = len(checked)
    with_new_activity = 0
    new_finds = 0
    for repo in all_repos:
        name = repo["full_name"]
        cached_pushed = pushed_map.get(name, "")
        current_pushed = repo.get("pushed_at", "")
        if name in active_cache:
            if current_pushed and cached_pushed and current_pushed > cached_pushed:
                with_new_activity += 1
                repo["new_activity"] = True
                repo["old_pushed"] = cached_pushed
        else:
            new_finds += 1
            repo["new_find"] = True

    # Update cache with all findings (including pushed_at)
    updated_pushed = {}
    for r in all_repos:
        if r.get("pushed_at"):
            updated_pushed[r["full_name"]] = r["pushed_at"]

    save_cache_v2(seen, updated_pushed, checked_set=discovered)

    extra_stats = {
        "re_evaluated": re_evaluated,
        "with_new_activity": with_new_activity,
        "new_finds": new_finds,
        "total_in_scope": len(seen),
        # Coverage, stated honestly. Cache size is inventory, not checks.
        "cache_size": len(active_cache),
        "attempted": len(discovered),
        "checked": len(checked),
        "unavailable": len(unavailable),
        "not_queried": len(not_queried),
    }
    print(
        f"WEEKLY: {len(checked)} of {len(active_cache)} tracked repos checked, "
        f"{len(unavailable)} not returned this run, "
        f"{with_new_activity} with new activity, {new_finds} new finds",
        file=sys.stderr,
    )
    return all_repos, extra_stats


# ── Filtering ──────────────────────────────────────────────────────


def filter_repos(repos, thresholds):
    """Stage 2: Pre-filtering using tuned thresholds.
    Returns (keep, filtered_out_with_reasons).
    """
    keep = []
    filtered = defaultdict(list)

    for repo in repos:
        is_noise, reason = classify_noise(repo, thresholds)
        if is_noise:
            filtered[reason].append(repo["full_name"])
        else:
            keep.append(repo)

    print(f"FILTER: {len(keep)} kept, {len(repos) - len(keep)} filtered:", file=sys.stderr)
    for reason, names in sorted(filtered.items()):
        print(f"  {reason}: {len(names)}", file=sys.stderr)

    return keep, dict(filtered)


def deduplicate_repos(repos):
    """Stage 2b: Deduplication."""
    result = deduplicate(repos)
    print(
        f"DEDUP: {len(result)} unique after dedup "
        f"({len(repos) - len(result)} duplicates removed)",
        file=sys.stderr,
    )
    return result


# ── Text Summary (clean, human-readable) ───────────────────────────


def _truncate(text, n):
    if not text:
        return ""
    return text if len(text) <= n else text[: n - 1] + "…"


def build_text_summary(repos, stats, extra_stats):
    """Build a clean, human-readable text summary (not Discord markdown)."""
    lines = [
        f"=== GitHub Radar · {DATE_STR} {TIME_STR} ({MODE.upper()}) ===",
        "",
        "Configuration",
        f"  Mode: {MODE}",
        f"  Star threshold: {stats['active_threshold']}",
        f"  Queries: {stats['collection_queries']}",
        "",
    ]

    # Mode-aware collection section
    if MODE == "weekly" and extra_stats:
        lines.extend([
            "Re-evaluation",
            f"  Repos re-evaluated: {extra_stats.get('re_evaluated', 0)}",
            f"  With new activity: {extra_stats.get('with_new_activity', 0)} (pushed_at advanced since last check)",
            f"  New finds: {extra_stats.get('new_finds', 0)}",
            f"  In scope: {extra_stats.get('total_in_scope', 0)}",
            "",
        ])
    else:
        lines.extend([
            "Collection",
            f"  Total new finds: {stats['total_collected']}",
            f"    From API: {extra_stats.get('new_api', 0)}",
            f"    From trending: {extra_stats.get('new_trending', 0)}",
            f"  Cached active: {extra_stats.get('cached_active', 0)} (TTL={CACHE_TTL_DAYS}d)",
            "",
        ])

    # Filtering
    lines.append("Filtering")
    lines.append(f"  Kept: {stats['after_dedup']}")
    lines.append(f"  Noise: {stats['noise']}")
    for reason, count in sorted(stats.get("filter_reasons", {}).items()):
        lines.append(f"    {reason}: {count}")
    lines.append("")

    # Tuning
    lines.append("Tuning")
    for action in stats.get("tuning_actions", []):
        lines.append(f"  • {action}")
    lines.append("")

    # Top repos
    top_n = 5
    top = repos[:top_n]
    if top:
        lines.append(f"Top {len(top)} by score")
        for i, r in enumerate(top, 1):
            prefix = ""
            if r.get("new_activity"):
                prefix = "[NEW ACTIVITY] "
            elif r.get("new_find"):
                prefix = "[NEW FIND] "
            score = r.get("score", 0)
            # Unknown metadata must read as unknown, never as a measured 0.
            stars = r.get("stars")
            stars = "unknown" if stars is None else stars
            lang = r.get("language") or "—"
            desc = _truncate(r.get("description", ""), 70)
            lines.append(
                f"  {i}. {prefix}{r['full_name']}  ★{stars}  score {score}  ({lang})"
            )
            if desc:
                lines.append(f"     {desc}")
        lines.append("")

    lines.append(
        f"Full results: data/discoveries.json ({stats['after_dedup']} repos)"
    )
    return "\n".join(lines)


# ── Main ───────────────────────────────────────────────────────────


def main():
    """Full pipeline: collect → filter → dedup → score → tune → output."""
    # Load thresholds
    thresholds = load_thresholds()
    queries = build_queries(thresholds)

    print(
        f"CONFIG: mode={MODE}, star_threshold={thresholds['star_threshold']}, "
        f"queries={len(queries)}",
        file=sys.stderr,
    )

    # Stage 1: Collection (mode-dependent)
    if MODE == "weekly":
        all_repos, extra_stats = collect_weekly(queries)
    else:
        all_repos, extra_stats = collect_daily(queries)

    # Stage 2: Pre-filtering
    filtered, filter_reasons = filter_repos(all_repos, thresholds)

    # Stage 2b: Dedup
    final = deduplicate_repos(filtered)

    # Stage 2c: Score + sort
    scored = score_all(final)

    # Compute metrics for this run
    total = len(all_repos)
    noise_count = total - len(filtered)
    signal_count = len(final)
    noise_rate_pct = round((noise_count / total * 100), 1) if total > 0 else 0.0
    signal_rate_pct = round((signal_count / total * 100), 1) if total > 0 else 0.0

    # Stage 3: Self-tuning from aggregated acquisition status only, as one
    # transaction so a concurrent writer cannot overwrite the decision.
    acquisition = acquisition_status()
    thresholds, tuning_actions = tune_and_publish(
        noise_rate_pct, signal_rate_pct, acquisition["worst"]
    )

    print("TUNING:", file=sys.stderr)
    for action in tuning_actions:
        print(f"  {action}", file=sys.stderr)

    # Build stats dict (passed to text summary and JSON)
    stats = {
        "total_collected": total,
        "after_filter": len(filtered),
        "after_dedup": len(final),
        "noise": noise_count,
        "noise_rate_pct": noise_rate_pct,
        "signal_rate_pct": signal_rate_pct,
        "active_threshold": thresholds["star_threshold"],
        "collection_queries": len(queries),
        "tuning_actions": tuning_actions,
        "filter_reasons": {k: len(v) for k, v in filter_reasons.items()},
    }

    # Build discoveries.json output
    output = {
        "collected_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "mode": MODE,
        "stats": stats,
        "extra_stats": extra_stats,
        "tuning": {
            "actions": tuning_actions,
            "thresholds": {
                "star_threshold": thresholds["star_threshold"],
                "noise_keywords_count": len(thresholds.get("noise_keywords", [])),
                "language_filters": thresholds.get("language_filters", []),
            },
        },
        "repos": scored,
    }

    # Publish atomically, and only when the run is complete. Both modes share
    # discoveries.json, so a failed scan used to overwrite the last good
    # snapshot with an ordinary-looking empty result. A partial run is written
    # beside it for diagnosis and the published file is left alone.
    os.makedirs(DATA_DIR, exist_ok=True)
    generation = new_generation_id()
    listed = len([r for r in scored if r.get("classification") != "NOISE"])
    if acquisition["partial"]:
        # A failed attempt must not displace the committed generation.
        atomic_write_json(ATTEMPT_FILE, {
            "generation": generation, "mode": MODE, "date": DATE_STR,
            "status": "partial", "acquisition_outcome": acquisition["worst"],
            "acquisition_counts": acquisition["counts"],
            "repos_found": len(scored), "repos_listed": listed,
        })
        print(f"PUBLISH: partial attempt — {OUTPUT_FILE} kept as last-good", file=sys.stderr)
    else:
        atomic_write_json(OUTPUT_FILE, output)
        json_sha = hashlib.sha256(
            json.dumps(output, indent=2, default=str).encode("utf-8")).hexdigest()
        manifest = {
            "schema": 3,
            "generation": generation,
            "mode": MODE,
            "date": DATE_STR,
            "run_id": generation,
            "collected_at": output["collected_at"],
            "status": "complete",
            "acquisition_outcome": acquisition["worst"],
            "acquisition_counts": acquisition["counts"],
            "partial": False,
            "tuning_eligible": acquisition["worst"] == ACQ_OK,
            "repos_found": len(scored),
            "repos_listed": listed,
            "discoveries_sha256": json_sha,
        }
        atomic_write_json(MANIFEST_FILE, manifest)
        manifest_sha = hashlib.sha256(
            json.dumps(manifest, indent=2, default=str).encode("utf-8")).hexdigest()
        atomic_write_json(COMMIT_FILE, {
            "schema": 1,
            "generation": generation,
            "mode": MODE,
            "date": DATE_STR,
            "status": "complete",
            "discoveries_file": os.path.basename(OUTPUT_FILE),
            "discoveries_sha256": hashlib.sha256(
                open(OUTPUT_FILE, "rb").read()).hexdigest(),
            "manifest_file": os.path.basename(MANIFEST_FILE),
            "manifest_sha256": manifest_sha,
            "repos_found": len(scored),
            "repos_listed": listed,
        })

    # Print clean human-readable summary to stdout
    print(build_text_summary(scored, stats, extra_stats))

    # Append metrics entry for next run's tuning loop
    metrics_entry = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "mode": MODE,
        "total_repos": total,
        "actionable": signal_count,
        "filter_accepted": signal_count,
        "metric_label_note": METRIC_LABEL_NOTE,
        "noise": noise_count,
        "signal_rate_pct": signal_rate_pct,
        "noise_rate_pct": noise_rate_pct,
        "star_threshold": thresholds["star_threshold"],
    }
    if MODE == "weekly" and extra_stats:
        metrics_entry["re_evaluated"] = extra_stats.get("re_evaluated", 0)
        metrics_entry["with_new_activity"] = extra_stats.get("with_new_activity", 0)
        metrics_entry["new_finds"] = extra_stats.get("new_finds", 0)
    elif extra_stats:
        metrics_entry["cached_total"] = extra_stats.get("total", 0)
        metrics_entry["new_trending"] = extra_stats.get("new_trending", 0)
    append_metrics_entry(metrics_entry)


if __name__ == "__main__":
    main()
