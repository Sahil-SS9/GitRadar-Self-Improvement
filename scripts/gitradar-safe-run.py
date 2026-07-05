#!/usr/bin/env python3
"""
Cron-safe GitRadar runner.

Runs discovery, validates the generated discoveries.json, and optionally runs the
scoring/classification step with a bounded ranked recommendation output. Use this
wrapper from cron/systemd instead of calling gitradar-discover.py directly: a
suspicious JSON output should fail loud before downstream agents trust it, and
scheduled jobs should not dump thousands of weakly-ranked candidates by default.
"""

import argparse
import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
DATA_DIR = os.path.join(REPO_ROOT, "data")
DISCOVER = os.path.join(SCRIPT_DIR, "gitradar-discover.py")
VALIDATE = os.path.join(SCRIPT_DIR, "gitradar-validate.py")
SCORE = os.path.join(SCRIPT_DIR, "gitradar-score.py")
DISCOVERIES = os.path.join(DATA_DIR, "discoveries.json")
RECOMMENDATIONS = os.path.join(DATA_DIR, "recommendations.json")


def run_step(cmd, label):
    print(f"RUN: {label}: {' '.join(cmd)}", file=sys.stderr)
    return subprocess.run(cmd, cwd=REPO_ROOT)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run GitRadar with post-run validation.")
    parser.add_argument("--mode", choices=["daily", "weekly"], default="daily")
    parser.add_argument("--python", default=sys.executable, help="Python executable to use for child scripts")
    parser.add_argument("--min-repos", type=int, default=0, help="Fail validation if fewer than this many repos survive")
    parser.add_argument("--fail-on-empty", action="store_true", help="Fail validation if discovery collects zero repos")
    parser.add_argument("--recommendation-limit", type=int, default=100,
                        help="Maximum scored recommendations to write after ranking (0 = unlimited)")
    parser.add_argument("--min-score", type=float, default=70.0,
                        help="Drop scored recommendations below this score threshold")
    parser.add_argument("--skip-score", action="store_true", help="Only discover+validate; do not run gitradar-score.py")
    args = parser.parse_args()

    discover_cmd = [args.python, DISCOVER]
    if args.mode != "daily":
        discover_cmd += ["--mode", args.mode]
    result = run_step(discover_cmd, "discover")
    if result.returncode != 0:
        print(f"FAIL: discovery exited {result.returncode}", file=sys.stderr)
        return result.returncode

    validate_cmd = [args.python, VALIDATE, "--input", DISCOVERIES, "--min-repos", str(args.min_repos)]
    if args.fail_on_empty:
        validate_cmd.append("--fail-on-empty")
    result = run_step(validate_cmd, "validate")
    if result.returncode != 0:
        print(f"FAIL: validation exited {result.returncode}", file=sys.stderr)
        return result.returncode

    if not args.skip_score:
        score_cmd = [
            args.python,
            SCORE,
            "--input",
            DISCOVERIES,
            "--output",
            RECOMMENDATIONS,
            "--limit",
            str(args.recommendation_limit),
            "--min-score",
            str(args.min_score),
        ]
        result = run_step(score_cmd, "score")
        if result.returncode != 0:
            print(f"FAIL: scoring exited {result.returncode}", file=sys.stderr)
            return result.returncode

    print("OK: GitRadar safe run completed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
