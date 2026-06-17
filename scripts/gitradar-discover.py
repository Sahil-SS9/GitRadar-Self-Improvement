#!/usr/bin/env python3
"""
GitHub Radar — Automated Repo Discovery Pipeline (Daily Cron)
Phase 1-3: Discovery + Classification + Kanban Routing + Digest

Collects repos from GitHub Search API + trending scrape,
pre-filters noise, self-tunes thresholds based on signal quality,
then outputs structured JSON for the Hermes cron agent.

v3.2.0 — Added self-tuning thresholds feedback loop.
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.parse
from collections import defaultdict
from datetime import datetime, timezone, timedelta

# ── Data Paths ──────────────────────────────────────────────────────

# Always use local data/ directory for community edition
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
CACHE_FILE = os.path.join(DATA_DIR, "cache.json")
OUTPUT_FILE = os.path.join(DATA_DIR, "discoveries.json")
METRICS_FILE = os.path.join(DATA_DIR, "metrics.json")
THRESHOLDS_FILE = os.path.join(DATA_DIR, "thresholds.json")

# ── Static Config ───────────────────────────────────────────────────

RECENCY_DAYS = 7
CREATED_RECENCY_DAYS = 90  # wider window to exclude truly ancient repos
MAX_RESULTS_PER_QUERY = 100
MAX_PAGES = 10  # GitHub caps search results at 1000
TRENDING_URL = "https://github.com/trending?since=daily"

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
]

# ── Thresholds ──────────────────────────────────────────────────────

DEFAULT_THRESHOLDS = {
    "star_threshold": 100,
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


def load_thresholds():
    """Load thresholds.json, falling back to defaults with a fresh history entry."""
    if not os.path.exists(THRESHOLDS_FILE):
        save_thresholds(DEFAULT_THRESHOLDS)
        return dict(DEFAULT_THRESHOLDS)
    try:
        with open(THRESHOLDS_FILE) as f:
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
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(THRESHOLDS_FILE, "w") as f:
            json.dump(thresholds, f, indent=2, default=str)
    except OSError as e:
        print(f"WARN: Failed to write thresholds: {e}", file=sys.stderr)


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
        with open(METRICS_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def append_metrics_entry(entry):
    """Append a metrics entry, prune to 365 max."""
    metrics = load_metrics()
    metrics.append(entry)
    # Prune to 365 entries
    if len(metrics) > 365:
        metrics = metrics[-365:]
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(METRICS_FILE, "w") as f:
            json.dump(metrics, f, indent=2)
    except OSError as e:
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


def self_tune(thresholds, noise_rate_pct, signal_rate_pct):
    """
    Read recent metrics and adjust thresholds based on signal quality.
    Returns (updated_thresholds, tuning_actions_log) where tuning_actions_log
    is a list of human-readable strings describing what changed.
    """
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


# ── Helpers ─────────────────────────────────────────────────────────


_RATE_LIMITED = False

def is_rate_limited():
    return _RATE_LIMITED

def reset_rate_limited():
    global _RATE_LIMITED
    _RATE_LIMITED = False

def set_rate_limited():
    global _RATE_LIMITED
    _RATE_LIMITED = True

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
        result = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            print(f"WARN: gh auth failed: {result.stderr.strip()}", file=sys.stderr)
            gh_auth_token._token = None
        else:
            gh_auth_token._token = result.stdout.strip()
    return gh_auth_token._token


def github_search(query, sort="stars", order="desc", per_page=100, page=1):
    """Call GitHub Search API. Returns (items, total_count) or ([], 0)."""
    token = gh_auth_token()
    if not token:
        return [], 0

    pushed_q, created_q = get_date_filters()
    full_q = f"{query} pushed:>{pushed_q} created:>{created_q}"
    params = urllib.parse.urlencode({
        "q": full_q, "sort": sort, "order": order,
        "per_page": per_page, "page": page
    })
    url = f"https://api.github.com/search/repositories?{params}"

    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "GitRadar/1.0")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            items = data.get("items", [])
            return items, data.get("total_count", 0)
    except urllib.error.HTTPError as e:
        print(f"WARN: GitHub API error {e.code} for query '{query[:60]}': {e.reason}", file=sys.stderr)
        if e.code in (403, 429):
            set_rate_limited()
        return [], 0
    except Exception as e:
        print(f"WARN: GitHub API exception: {e}", file=sys.stderr)
        return [], 0


def scrape_trending():
    """Scrape GitHub Trending page. Returns list of {full_name, ...}."""
    results = []
    try:
        req = urllib.request.Request(TRENDING_URL, headers={
            "User-Agent": "GitRadar/1.0",
            "Accept": "text/html",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        repos = set()
        for match in re.finditer(
            r'<h2[^>]*class="[^\"]*h3[^\"]*"[^>]*>.*?<a[^>]*href="/([^/\"]+)/([^/\"]+)"',
            html, re.DOTALL
        ):
            owner = match.group(1)
            repo = match.group(2)
            if owner and repo:
                repos.add(f"{owner}/{repo}")

        for full_name in repos:
            results.append({
                "full_name": full_name,
                "source": "trending",
            })
    except Exception as e:
        print(f"WARN: Trending scrape failed: {e}", file=sys.stderr)
    return results


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
        "dead_repo": lambda r: (
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


# ── Cache ───────────────────────────────────────────────────────────

CACHE_TTL_DAYS = 14  # repos expire from cache after 14 days, allowing re-discovery


def load_cache():
    """Load cache and prune entries older than CACHE_TTL_DAYS.
    Supports both old format {seen: [list]} and new format {seen: {repo: date_str}}."""
    if not os.path.exists(CACHE_FILE):
        return set()
    try:
        with open(CACHE_FILE) as f:
            data = json.load(f)
        seen_data = data.get("seen", [])
        # Old format: list of repo names (no timestamps) — clear and re-discover
        if isinstance(seen_data, list):
            print(f"CACHE: old list format ({len(seen_data)} entries), clearing (TTL upgrade)", file=sys.stderr)
            return set()
        # New format: dict of {repo: date_seen_str}
        if isinstance(seen_data, dict):
            now = datetime.now(timezone.utc)
            expired = 0
            active = set()
            for repo, date_str in seen_data.items():
                try:
                    seen_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                    if (now - seen_date).days < CACHE_TTL_DAYS:
                        active.add(repo)
                    else:
                        expired += 1
                except (ValueError, TypeError):
                    expired += 1
            if expired > 0:
                print(f"CACHE: pruned {expired} expired entries (TTL={CACHE_TTL_DAYS}d), {len(active)} active", file=sys.stderr)
            return active
    except (json.JSONDecodeError, KeyError):
        return set()
    return set()


def save_cache(seen):
    """Save cache in new timestamped format: {seen: {repo: date_seen_str}}."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        now_str = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        seen_dict = {repo: now_str for repo in seen}
        with open(CACHE_FILE, "w") as f:
            json.dump({"seen": seen_dict}, f)
    except Exception as e:
        print(f"WARN: Failed to write cache: {e}", file=sys.stderr)


# ── Main Pipeline ──────────────────────────────────────────────────


def collect(queries):
    """Stage 1: Data Collection. Returns raw list of repos."""
    reset_rate_limited()
    seen_cache = load_cache()
    seen = set(seen_cache)  # mutable working set
    all_repos = []

    # ── Primary + Secondary API searches ──
    for query_def in queries:
        if is_rate_limited():
            break
        q = query_def["q"]
        sort = query_def.get("sort", "stars")
        order = query_def.get("order", "desc")

        page = 1
        while page <= MAX_PAGES and not is_rate_limited():
            items, total = github_search(q, sort, order, 100, page)
            if is_rate_limited():
                break
            if not items:
                break
            for item in items:
                full_name = item.get("full_name", "")
                if full_name and full_name not in seen:
                    seen.add(full_name)
                    all_repos.append(extract_repo(item))
            page += 1
            # Be kind to the API
            if page <= MAX_PAGES:
                time.sleep(0.5)

    # ── Trending scrape (secondary source) ──
    trending = scrape_trending()
    for t in trending:
        name = t["full_name"]
        if name not in seen:
            seen.add(name)
            all_repos.append({
                "full_name": name,
                "description": "",
                "stars": 0,
                "forks": 0,
                "language": "",
                "topics": [],
                "created_at": "",
                "pushed_at": "",
                "open_issues": 0,
                "license": "",
                "html_url": f"https://github.com/{name}",
                "source": "trending",
            })

    # Save updated cache
    save_cache(seen)

    print(f"COLLECT: {len(all_repos)} repos collected "
          f"({len([r for r in all_repos if r['source'] == 'trending'])} from trending)",
          file=sys.stderr)

    return all_repos


def filter_repos(repos, thresholds):
    """Stage 2: Pre-filtering using tuned thresholds.
    Returns (keep, filtered_out_with_reasons)."""
    keep = []
    filtered = defaultdict(list)

    for repo in repos:
        is_noise, reason = classify_noise(repo, thresholds)
        if is_noise:
            filtered[reason].append(repo["full_name"])
        else:
            keep.append(repo)

    print(f"FILTER: {len(keep)} kept, {len(repos) - len(keep)} filtered:",
          file=sys.stderr)
    for reason, names in sorted(filtered.items()):
        print(f"  {reason}: {len(names)}", file=sys.stderr)

    return keep, dict(filtered)


def deduplicate_repos(repos):
    """Stage 2b: Deduplication."""
    result = deduplicate(repos)
    print(f"DEDUP: {len(result)} unique after dedup "
          f"({len(repos) - len(result)} duplicates removed)",
          file=sys.stderr)
    return result


def run_pipeline():
    """Full pipeline: collect -> filter -> dedup -> tune -> output JSON."""

    # Load thresholds
    thresholds = load_thresholds()
    queries = build_queries(thresholds)

    print(f"CONFIG: star_threshold={thresholds['star_threshold']}, "
          f"queries={len(queries)}",
          file=sys.stderr)

    # Stage 1: Collection
    all_repos = collect(queries)

    # Stage 2: Pre-filtering
    filtered, filter_reasons = filter_repos(all_repos, thresholds)

    # Stage 2b: Dedup
    final = deduplicate_repos(filtered)

    # Compute metrics for this run
    total = len(all_repos)
    noise_count = total - len(filtered)
    signal_count = len(final)
    noise_rate_pct = round((noise_count / total * 100), 1) if total > 0 else 0.0
    # Signal rate: repos that passed all filters / total collected
    signal_rate_pct = round((signal_count / total * 100), 1) if total > 0 else 0.0

    # Stage 3: Self-tuning
    thresholds, tuning_actions = self_tune(thresholds, noise_rate_pct, signal_rate_pct)
    save_thresholds(thresholds)

    print("TUNING:", file=sys.stderr)
    for action in tuning_actions:
        print(f"  {action}", file=sys.stderr)

    # Build output
    output = {
        "collected_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "stats": {
            "total_collected": total,
            "after_filter": len(filtered),
            "after_dedup": len(final),
            "collection_queries": len(queries),
            "noise_rate_pct": noise_rate_pct,
            "signal_rate_pct": signal_rate_pct,
            "active_threshold": thresholds["star_threshold"],
        },
        "filter_reasons": {k: len(v) for k, v in filter_reasons.items()},
        "tuning": {
            "actions": tuning_actions,
            "thresholds": {
                "star_threshold": thresholds["star_threshold"],
                "noise_keywords_count": len(thresholds.get("noise_keywords", [])),
                "language_filters": thresholds.get("language_filters", []),
            },
        },
        "repos": final,
    }

    # Write full output to disk
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2)

    # Stdout output (capped at 200 repos for cron token budget)
    output_stdout = dict(output)
    output_stdout["repos"] = final[:200]
    if len(final) > 200:
        output_stdout["stats"]["truncated"] = True
        output_stdout["stats"]["showing"] = 200

    print(json.dumps(output_stdout))

    # Append metrics entry for next run's tuning loop
    metrics_entry = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "total_repos": total,
        "actionable": signal_count,
        "noise": noise_count,
        "signal_rate_pct": signal_rate_pct,
        "noise_rate_pct": noise_rate_pct,
        "star_threshold": thresholds["star_threshold"],
    }
    append_metrics_entry(metrics_entry)

    return output


if __name__ == "__main__":
    run_pipeline()
