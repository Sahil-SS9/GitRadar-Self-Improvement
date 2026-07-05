#!/usr/bin/env python3
"""
GitRadar Scoring and Classification Script
Reads discoveries.json from the discovery step, scores each repo, filters/sorts
ranked candidates, classifies into labels (ADOPT, EXTRACT, FORK/PRODUCT,
PLUGIN/SKILL, INSPIRATION, NOISE), and outputs recommendations.json.

Usage:
    python3 scripts/gitradar-score.py [--input DISCOVERIES_JSON] [--output RECOMMENDATIONS_JSON] [--limit N] [--min-score N]

Defaults:
    --input: data/discoveries.json
    --output: data/recommendations.json
    --limit: 0 (unlimited)
    --min-score: 0.0
"""

import json
import os
import sys
import argparse
from datetime import datetime, timezone
import math

# Paths
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
STACK_FILE = os.path.join(os.path.dirname(__file__), "..", "config", "stack.json")

def load_stack():
    """Load stack preferences from config/stack.json, merging with defaults for safety."""
    # Built-in defaults (same as in config/stack.json)
    defaults = {
        "name": "default",
        "description": "Default tech stack preferences for scoring",
        "languages": {
            "python": 30,
            "typescript": 30,
            "javascript": 20,
            "rust": 25,
            "go": 20,
            "kotlin": 15,
            "swift": 15,
            "c++": 10,
            "java": 10,
            "shell": 5,
            "lua": 10,
            "zig": 15
        },
        "frameworks": {
            "react": 30,
            "react-native": 35,
            "expo": 30,
            "flutter": 25,
            "nextjs": 20,
            "vue": 15,
            "django": 15,
            "fastapi": 20
        },
        "ecosystem_keywords": [
            "mcp",
            "agent-framework",
            "agent-sdk",
            "developer-tools",
            "ai-agents",
            "llm",
            "llmops",
            "prompt-engineering",
            "function-calling",
            "tool-use",
            "cli",
            "automation",
            "devops",
            "react-native",
            "expo",
            "convex",
            "supabase",
            "flutter",
            "typescript",
            "python",
            "rust"
        ],
        "topic_bonus": 10,
        "license_preferences": {
            "MIT": 10,
            "Apache-2.0": 9,
            "BSD-2-Clause": 8,
            "BSD-3-Clause": 8,
            "MIT-0": 10,
            "0BSD": 8,
            "Unlicense": 7,
            "CC0-1.0": 6,
            "GPL-2.0": 4,
            "GPL-3.0": 3,
            "AGPL-3.0": 2,
            "LGPL-2.1": 5,
            "LGPL-3.0": 5,
            "MPL-2.0": 6,
            "BSL-1.0": 5
        },
        "noise_description_keywords": [
            "mod menu", "hack", "cheat", "spoofer", "aimbot", "wallhack",
            "unlocker", "crack", "cracked", "free download", "spammer"
        ]
    }
    try:
        with open(STACK_FILE) as f:
            user_stack = json.load(f)
        # Deep merge: user values override defaults
        def deep_merge(base, update):
            for key, value in update.items():
                if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                    deep_merge(base[key], value)
                else:
                    base[key] = value
        deep_merge(defaults, user_stack)
        return defaults
    except (json.JSONDecodeError, OSError):
        # If file missing or invalid, return hard-coded defaults
        return defaults

def load_json_file(filepath):
    """Load a JSON file, return empty dict if not found or invalid."""
    if not os.path.exists(filepath):
        return {}
    try:
        with open(filepath) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

def parse_date(date_str):
    """Parse ISO date string to datetime object."""
    if not date_str:
        return None
    try:
        # Handle Z suffix and timezone
        if date_str.endswith('Z'):
            date_str = date_str[:-1] + '+00:00'
        return datetime.fromisoformat(date_str)
    except Exception:
        return None

def stars_score(stars):
    """Score based on stars using log scale."""
    if stars <= 0:
        return 0.0
    # log10(stars+1) * 5, capped at 20
    return min(20.0, math.log10(stars + 1) * 5)

def recency_score(created_at_str):
    """Score based on how recent the repo is."""
    created_at = parse_date(created_at_str)
    if not created_at:
        return 0.0
    now = datetime.now(timezone.utc)
    days_old = (now - created_at).days
    if days_old < 7:
        return 15.0
    elif days_old < 30:
        return 10.0
    elif days_old < 90:
        return 5.0
    else:
        return 0.0

def language_score(language, stack):
    """Score based on language match."""
    if not language:
        return 0.0
    language_key = str(language).lower()
    return float(stack["languages"].get(language_key, 0))

def framework_score(topics, stack):
    """Score based on framework match in topics."""
    if not topics:
        return 0.0
    frameworks = stack["frameworks"]
    scores = (frameworks.get(str(topic).lower(), 0) for topic in topics)
    return float(max(scores, default=0))

def description_score(description, stack):
    """Score based on description quality and noise keywords."""
    if not description:
        return 0.0
    desc_lower = description.lower()
    for keyword in stack["noise_description_keywords"]:
        if keyword in desc_lower:
            return 0.0  # Noise keyword found -> zero for description
    # If we get here, description is non-empty and no noise keywords
    return 10.0

def license_score(license_str, stack):
    """Score based on license."""
    if not license_str:
        return 0.0
    return float(stack["license_preferences"].get(license_str, 0))

def topic_bonus_score(topics, stack):
    """Bonus for ecosystem keywords in topics."""
    if not topics:
        return 0.0
    for topic in topics:
        if topic in stack["ecosystem_keywords"]:
            return float(stack["topic_bonus"])
    return 0.0

def compute_score(repo, stack):
    """Compute the total score for a repo."""
    # Stars
    s_score = stars_score(repo.get("stars", 0))
    # Recency
    r_score = recency_score(repo.get("created_at", ""))
    # Language
    l_score = language_score(repo.get("language"), stack)
    # Framework
    f_score = framework_score(repo.get("topics", []), stack)
    # Description
    d_score = description_score(repo.get("description", ""), stack)
    # License
    lic_score = license_score(repo.get("license"), stack)
    # Topic bonus
    t_score = topic_bonus_score(repo.get("topics", []), stack)
    
    total = s_score + r_score + l_score + f_score + d_score + lic_score + t_score
    # Clamp between 0 and 100
    return max(0.0, min(100.0, total))

def classify_repo(score):
    """Classify repo based on score."""
    if score >= 80:
        return "ADOPT"
    elif score >= 60:
        return "EXTRACT"
    elif score >= 50:
        return "FORK/PRODUCT"
    elif score >= 40:
        return "PLUGIN/SKILL"
    elif score >= 20:
        return "INSPIRATION"
    else:
        return "NOISE"


def score_repositories(repos, stack, min_score=0.0, limit=0):
    """Score, filter, sort, and optionally cap repositories.

    A 0 limit means unlimited. Sorting happens after score computation so the
    output is a ranked recommendation list rather than discovery-order JSON.
    """
    scored_repos = []
    for repo in repos:
        score = compute_score(repo, stack)
        if score < min_score:
            continue
        label = classify_repo(score)
        repo_with_score = dict(repo)  # Shallow copy
        repo_with_score["score"] = round(score, 2)
        repo_with_score["label"] = label
        scored_repos.append(repo_with_score)

    scored_repos.sort(
        key=lambda repo: (
            float(repo.get("score") or 0),
            int(repo.get("stars") or 0),
            str(repo.get("pushed_at") or ""),
            str(repo.get("full_name") or ""),
        ),
        reverse=True,
    )
    if limit and limit > 0:
        return scored_repos[:limit]
    return scored_repos


def main():
    parser = argparse.ArgumentParser(description="Score and classify GitRadar discoveries.")
    parser.add_argument("--input", default=os.path.join(DATA_DIR, "discoveries.json"),
                        help="Input discoveries JSON file (default: data/discoveries.json)")
    parser.add_argument("--output", default=os.path.join(DATA_DIR, "recommendations.json"),
                        help="Output recommendations JSON file (default: data/recommendations.json)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Maximum recommendations to write after ranking (0 = unlimited)")
    parser.add_argument("--min-score", type=float, default=0.0,
                        help="Drop repos with computed score below this threshold")
    args = parser.parse_args()

    # Load stack preferences
    stack = load_stack()
    # Load discoveries
    discoveries = load_json_file(args.input)
    if not discoveries or "repos" not in discoveries:
        print(f"ERROR: Could not load discoveries from {args.input}", file=sys.stderr)
        sys.exit(1)

    repos = discoveries.get("repos", [])
    scored_repos = score_repositories(
        repos,
        stack,
        min_score=args.min_score,
        limit=args.limit,
    )

    # Build output
    output = {
        "collected_at": discoveries.get("collected_at"),
        "source_total_repos": len(repos),
        "total_repos": len(scored_repos),
        "min_score": args.min_score,
        "limit": args.limit,
        "repos": scored_repos
    }

    # Write output
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(output, f, indent=2)

    # Print summary to stderr
    label_counts = {}
    for repo in scored_repos:
        label = repo["label"]
        label_counts[label] = label_counts.get(label, 0) + 1

    print(
        f"SCORED: {len(scored_repos)} recommendations "
        f"from {len(repos)} repos (min_score={args.min_score}, limit={args.limit or 'unlimited'})",
        file=sys.stderr,
    )
    for label, count in sorted(label_counts.items()):
        print(f"  {label}: {count}", file=sys.stderr)

if __name__ == "__main__":
    main()