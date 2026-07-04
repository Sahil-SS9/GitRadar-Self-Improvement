#!/usr/bin/env python3
"""
GitRadar output validator.

Cron jobs should not blindly trust a JSON file just because discovery exited 0.
This validator checks the structural invariants that make downstream scoring or
agent review safe: required top-level keys, stats consistency, required repo
fields, and a high-signal regression where enriched trending repos collapse into
zero-star skeletons.
"""

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Tuple

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
DEFAULT_INPUT = os.path.join(DATA_DIR, "discoveries.json")

REQUIRED_TOP_LEVEL = {"collected_at", "mode", "stats", "extra_stats", "repos"}
REQUIRED_STATS = {
    "total_collected",
    "after_filter",
    "after_dedup",
    "noise",
    "noise_rate_pct",
    "signal_rate_pct",
    "active_threshold",
    "collection_queries",
}
REQUIRED_REPO_FIELDS = {
    "full_name",
    "description",
    "stars",
    "forks",
    "language",
    "topics",
    "created_at",
    "pushed_at",
    "open_issues",
    "license",
    "html_url",
    "source",
    "score",
}
VALID_MODES = {"daily", "weekly"}


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_trending_skeleton(repo: Dict[str, Any]) -> bool:
    """Return True when a trending repo has been reduced to empty metadata."""
    source = str(repo.get("source", ""))
    if not source.startswith("trending"):
        return False
    return (
        int(repo.get("stars") or 0) == 0
        and not str(repo.get("description") or "").strip()
        and not str(repo.get("language") or "").strip()
        and not repo.get("topics")
        and not str(repo.get("pushed_at") or "").strip()
        and not str(repo.get("license") or "").strip()
    )


def validate_discoveries(
    payload: Dict[str, Any],
    *,
    min_repos: int = 0,
    fail_on_empty: bool = False,
) -> Tuple[List[str], List[str]]:
    """Validate a discoveries.json payload.

    Returns (errors, warnings). Errors are cron-stopping conditions. Warnings are
    suspicious but may be acceptable depending on schedule/cache state.
    """
    errors: List[str] = []
    warnings: List[str] = []

    if not isinstance(payload, dict):
        return ["payload must be a JSON object"], []

    missing_top = sorted(REQUIRED_TOP_LEVEL - set(payload))
    if missing_top:
        errors.append(f"missing top-level keys: {', '.join(missing_top)}")

    mode = payload.get("mode")
    if mode not in VALID_MODES:
        errors.append(f"mode must be one of {sorted(VALID_MODES)}, got {mode!r}")

    stats = payload.get("stats")
    if not isinstance(stats, dict):
        errors.append("stats must be an object")
        stats = {}
    else:
        missing_stats = sorted(REQUIRED_STATS - set(stats))
        if missing_stats:
            errors.append(f"missing stats keys: {', '.join(missing_stats)}")

    repos = payload.get("repos")
    if not isinstance(repos, list):
        errors.append("repos must be a list")
        repos = []

    extra_stats = payload.get("extra_stats")
    if not isinstance(extra_stats, dict):
        errors.append("extra_stats must be an object")
        extra_stats = {}

    if mode == "daily":
        required_extra = {"total", "new_api", "new_trending", "cached_active"}
        missing_extra = sorted(required_extra - set(extra_stats))
        if missing_extra:
            errors.append(f"missing daily extra_stats keys: {', '.join(missing_extra)}")
    elif mode == "weekly":
        required_extra = {"re_evaluated", "with_new_activity", "new_finds", "total_in_scope"}
        missing_extra = sorted(required_extra - set(extra_stats))
        if missing_extra:
            errors.append(f"missing weekly extra_stats keys: {', '.join(missing_extra)}")

    numeric_stats = [
        "total_collected",
        "after_filter",
        "after_dedup",
        "noise",
        "noise_rate_pct",
        "signal_rate_pct",
        "active_threshold",
        "collection_queries",
    ]
    for key in numeric_stats:
        if key in stats and not _is_number(stats.get(key)):
            errors.append(f"stats.{key} must be numeric")

    total_collected = int(stats.get("total_collected") or 0)
    after_filter = int(stats.get("after_filter") or 0)
    after_dedup = int(stats.get("after_dedup") or 0)
    noise = int(stats.get("noise") or 0)

    if after_filter + noise != total_collected:
        errors.append(
            "stats mismatch: after_filter + noise must equal total_collected "
            f"({after_filter} + {noise} != {total_collected})"
        )
    if after_dedup > after_filter:
        errors.append("stats mismatch: after_dedup cannot exceed after_filter")
    if len(repos) != after_dedup:
        errors.append(
            f"stats mismatch: len(repos) must equal after_dedup ({len(repos)} != {after_dedup})"
        )
    if len(repos) < min_repos:
        errors.append(f"repo count {len(repos)} below required minimum {min_repos}")
    if fail_on_empty and total_collected == 0:
        errors.append("empty collection while --fail-on-empty is set")
    elif total_collected == 0:
        warnings.append("collection returned zero repos; acceptable for cache-heavy runs, suspicious for first runs")

    seen_names = set()
    trending_skeletons = []
    for idx, repo in enumerate(repos):
        if not isinstance(repo, dict):
            errors.append(f"repos[{idx}] must be an object")
            continue
        missing_repo = sorted(REQUIRED_REPO_FIELDS - set(repo))
        if missing_repo:
            errors.append(f"repos[{idx}] missing fields: {', '.join(missing_repo)}")

        full_name = repo.get("full_name")
        if not isinstance(full_name, str) or "/" not in full_name:
            errors.append(f"repos[{idx}].full_name must be owner/name")
        elif full_name in seen_names:
            errors.append(f"duplicate repo in output: {full_name}")
        else:
            seen_names.add(full_name)

        if "stars" in repo and not isinstance(repo.get("stars"), int):
            errors.append(f"repos[{idx}].stars must be an integer")
        if "forks" in repo and not isinstance(repo.get("forks"), int):
            errors.append(f"repos[{idx}].forks must be an integer")
        if "open_issues" in repo and not isinstance(repo.get("open_issues"), int):
            errors.append(f"repos[{idx}].open_issues must be an integer")
        if "topics" in repo and not isinstance(repo.get("topics"), list):
            errors.append(f"repos[{idx}].topics must be a list")
        if "score" in repo and not _is_number(repo.get("score")):
            errors.append(f"repos[{idx}].score must be numeric")
        if _is_trending_skeleton(repo):
            trending_skeletons.append(str(full_name))

    if trending_skeletons:
        errors.append(
            "trending metadata collapsed to skeleton for: " + ", ".join(trending_skeletons[:10])
        )

    return errors, warnings


def load_json(path: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate GitRadar discoveries.json before cron/downstream use.")
    parser.add_argument("--input", default=DEFAULT_INPUT, help="Input discoveries JSON path")
    parser.add_argument("--min-repos", type=int, default=0, help="Fail if fewer than this many repos survive filtering")
    parser.add_argument("--fail-on-empty", action="store_true", help="Fail when collection returns zero repos")
    args = parser.parse_args()

    try:
        payload = load_json(args.input)
    except FileNotFoundError:
        print(f"INVALID: input not found: {args.input}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as e:
        print(f"INVALID: could not parse JSON: {e}", file=sys.stderr)
        return 2

    errors, warnings = validate_discoveries(
        payload,
        min_repos=args.min_repos,
        fail_on_empty=args.fail_on_empty,
    )
    for warning in warnings:
        print(f"WARN: {warning}", file=sys.stderr)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print(
        "VALID: "
        f"mode={payload.get('mode')} repos={len(payload.get('repos', []))} "
        f"noise={payload.get('stats', {}).get('noise', 0)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
