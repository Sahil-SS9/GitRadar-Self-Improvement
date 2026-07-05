import importlib.util
import os
import pathlib
import subprocess
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load {relative_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GitRadarDiscoverTests(unittest.TestCase):
    def setUp(self):
        self.discover = load_module("gitradar_discover", "scripts/gitradar-discover.py")

    def test_collect_daily_stops_api_scan_after_rate_limit(self):
        calls = []

        def fake_search(query, sort="stars", order="desc", per_page=100, page=1):
            calls.append(query)
            return [], -1

        queries = [
            {"q": "language:python stars:>50", "sort": "stars", "order": "desc"},
            {"q": "language:typescript stars:>50", "sort": "stars", "order": "desc"},
        ]

        with mock.patch.object(self.discover, "load_cache_v2", return_value=(set(), {})), \
             mock.patch.object(self.discover, "save_cache_v2"), \
             mock.patch.object(self.discover, "scrape_all_trending", return_value=[]), \
             mock.patch.object(self.discover, "enrich_trending_repos", return_value=[]), \
             mock.patch.object(self.discover, "github_search", side_effect=fake_search):
            repos, extra_stats = self.discover.collect_daily(queries)

        self.assertEqual(repos, [])
        self.assertEqual(calls, ["language:python stars:>50"])
        self.assertEqual(extra_stats["new_api"], 0)
        self.assertEqual(extra_stats["new_trending"], 0)

    def test_collect_daily_has_no_stale_rate_limit_state_between_calls(self):
        queries = [
            {"q": "language:python stars:>50", "sort": "stars", "order": "desc"}
        ]

        with mock.patch.object(self.discover, "load_cache_v2", return_value=(set(), {})), \
             mock.patch.object(self.discover, "save_cache_v2"), \
             mock.patch.object(self.discover, "scrape_all_trending", return_value=[]), \
             mock.patch.object(self.discover, "enrich_trending_repos", return_value=[]), \
             mock.patch.object(self.discover, "github_search", return_value=([], -1)):
            repos, _ = self.discover.collect_daily(queries)

        self.assertEqual(repos, [])

        with mock.patch.object(self.discover, "load_cache_v2", return_value=(set(), {})), \
             mock.patch.object(self.discover, "save_cache_v2"), \
             mock.patch.object(self.discover, "scrape_all_trending", return_value=[]), \
             mock.patch.object(self.discover, "enrich_trending_repos", return_value=[]), \
             mock.patch.object(self.discover, "github_search", return_value=([], 0)) as search:
            repos, _ = self.discover.collect_daily(queries)

        self.assertEqual(repos, [])
        self.assertEqual(search.call_count, 1)

    def test_gh_auth_token_prefers_environment_token(self):
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": " env-token \n"}, clear=False), \
             mock.patch.object(subprocess, "run") as run:
            token = self.discover.gh_auth_token()

        self.assertEqual(token, "env-token")
        run.assert_not_called()

    def test_trending_skeleton_is_filtered_as_dead_repo_by_default(self):
        repo = {
            "full_name": "owner/repo",
            "description": "",
            "stars": 0,
            "forks": 0,
            "language": "",
            "topics": [],
            "created_at": "",
            "pushed_at": "",
            "open_issues": 0,
            "license": "",
            "html_url": "https://github.com/owner/repo",
            "source": "trending",
        }

        self.assertEqual(
            self.discover.classify_noise(repo, self.discover.DEFAULT_THRESHOLDS),
            (True, "dead_repo"),
        )

    def test_enriched_trending_metadata_survives_collection(self):
        enriched = [{
            "full_name": "owner/useful-agent-tool",
            "description": "Useful agent automation CLI",
            "stars": 123,
            "forks": 4,
            "language": "Python",
            "topics": ["agent-framework", "cli"],
            "created_at": "2026-06-01T00:00:00Z",
            "pushed_at": "2026-07-01T00:00:00Z",
            "open_issues": 2,
            "license": "MIT",
            "html_url": "https://github.com/owner/useful-agent-tool",
            "source": "trending-daily",
        }]

        with mock.patch.object(self.discover, "load_cache_v2", return_value=(set(), {})), \
             mock.patch.object(self.discover, "save_cache_v2"), \
             mock.patch.object(self.discover, "scrape_all_trending", return_value=[{"full_name": "owner/useful-agent-tool"}]), \
             mock.patch.object(self.discover, "enrich_trending_repos", return_value=enriched), \
             mock.patch.object(self.discover, "github_search", return_value=([], 0)):
            repos, extra_stats = self.discover.collect_daily([])

        self.assertEqual(len(repos), 1)
        repo = repos[0]
        self.assertEqual(repo["stars"], 123)
        self.assertEqual(repo["language"], "Python")
        self.assertEqual(repo["topics"], ["agent-framework", "cli"])
        self.assertEqual(repo["license"], "MIT")
        self.assertEqual(repo["source"], "trending-daily")
        self.assertEqual(extra_stats["new_trending"], 1)
        self.assertEqual(
            self.discover.classify_noise(repo, self.discover.DEFAULT_THRESHOLDS),
            (False, ""),
        )

    def test_spam_name_filter_catches_farmed_repos_not_legit_names(self):
        def repo(full_name):
            return {
                "full_name": full_name,
                "description": "a real description",
                "stars": 200,
                "forks": 0,
                "language": "Go",
                "topics": [],
                "created_at": "",
                "pushed_at": "",
                "open_issues": 0,
                "license": "",
                "html_url": f"https://github.com/{full_name}",
                "source": "api",
            }

        th = self.discover.DEFAULT_THRESHOLDS
        for spam in ("x/FL-Product-Version-26", "x/WorpGPT-Latest-2026-AllPrompts",
                     "x/DeepFake-AI-2026-RealTime", "x/photoshop-crack-keygen"):
            self.assertEqual(self.discover.classify_noise(repo(spam), th), (True, "spam_name"), spam)
        for legit in ("meta/llama-3", "openai/gpt-4", "vercel/next.js", "anthropics/claude-mcp"):
            _, reason = self.discover.classify_noise(repo(legit), th)
            self.assertNotEqual(reason, "spam_name", legit)


class GitRadarValidateTests(unittest.TestCase):
    def setUp(self):
        self.validate = load_module("gitradar_validate", "scripts/gitradar-validate.py")

    def valid_payload(self):
        return {
            "collected_at": "2026-07-03T00:00:00Z",
            "mode": "daily",
            "stats": {
                "total_collected": 1,
                "after_filter": 1,
                "after_dedup": 1,
                "noise": 0,
                "noise_rate_pct": 0.0,
                "signal_rate_pct": 100.0,
                "active_threshold": 75,
                "collection_queries": 13,
            },
            "extra_stats": {
                "total": 1,
                "new_api": 0,
                "new_trending": 1,
                "cached_active": 0,
            },
            "repos": [{
                "full_name": "owner/useful-agent-tool",
                "description": "Useful agent automation CLI",
                "stars": 123,
                "forks": 4,
                "language": "Python",
                "topics": ["agent-framework", "cli"],
                "created_at": "2026-06-01T00:00:00Z",
                "pushed_at": "2026-07-01T00:00:00Z",
                "open_issues": 2,
                "license": "MIT",
                "html_url": "https://github.com/owner/useful-agent-tool",
                "source": "trending-daily",
                "score": 75.4,
            }],
        }

    def test_validate_accepts_good_payload(self):
        errors, warnings = self.validate.validate_discoveries(self.valid_payload())
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_validate_rejects_missing_schema_fields(self):
        payload = self.valid_payload()
        del payload["stats"]["after_dedup"]
        del payload["repos"][0]["html_url"]

        errors, _ = self.validate.validate_discoveries(payload)

        self.assertTrue(any("missing stats keys: after_dedup" in e for e in errors))
        self.assertTrue(any("missing fields: html_url" in e for e in errors))

    def test_validate_rejects_empty_output_when_required(self):
        payload = self.valid_payload()
        payload["stats"].update({
            "total_collected": 0,
            "after_filter": 0,
            "after_dedup": 0,
            "noise": 0,
            "noise_rate_pct": 0.0,
            "signal_rate_pct": 0.0,
        })
        payload["extra_stats"].update({"total": 0, "new_trending": 0})
        payload["repos"] = []

        errors, warnings = self.validate.validate_discoveries(payload, fail_on_empty=True)

        self.assertTrue(any("empty collection" in e for e in errors))
        self.assertEqual(warnings, [])

    def test_validate_warns_on_empty_output_when_not_required(self):
        payload = self.valid_payload()
        payload["stats"].update({
            "total_collected": 0,
            "after_filter": 0,
            "after_dedup": 0,
            "noise": 0,
            "noise_rate_pct": 0.0,
            "signal_rate_pct": 0.0,
        })
        payload["extra_stats"].update({"total": 0, "new_trending": 0})
        payload["repos"] = []

        errors, warnings = self.validate.validate_discoveries(payload)

        self.assertEqual(errors, [])
        self.assertTrue(any("zero repos" in w for w in warnings))

    def test_validate_rejects_trending_skeleton_regression(self):
        payload = self.valid_payload()
        payload["repos"][0].update({
            "description": "",
            "stars": 0,
            "language": "",
            "topics": [],
            "pushed_at": "",
            "license": "",
            "source": "trending-daily",
        })

        errors, _ = self.validate.validate_discoveries(payload)

        self.assertTrue(any("trending metadata collapsed to skeleton" in e for e in errors))

    def test_validate_rejects_stats_mismatch(self):
        payload = self.valid_payload()
        payload["stats"]["after_dedup"] = 2

        errors, _ = self.validate.validate_discoveries(payload)

        self.assertTrue(any("len(repos) must equal after_dedup" in e for e in errors))


class GitRadarScoreTests(unittest.TestCase):
    def setUp(self):
        self.score = load_module("gitradar_score", "scripts/gitradar-score.py")
        self.stack = self.score.load_stack()

    def test_language_score_handles_github_language_casing(self):
        self.assertEqual(self.score.language_score("Python", self.stack), 30.0)
        self.assertEqual(self.score.language_score("TypeScript", self.stack), 30.0)
        self.assertEqual(self.score.language_score("JavaScript", self.stack), 20.0)
        self.assertEqual(self.score.language_score("C++", self.stack), 10.0)

    def test_framework_score_uses_best_matching_framework_not_topic_order(self):
        self.assertEqual(
            self.score.framework_score(["nextjs", "expo", "react-native"], self.stack),
            35.0,
        )
        self.assertEqual(
            self.score.framework_score(["react", "expo", "nextjs"], self.stack),
            30.0,
        )
        self.assertEqual(
            self.score.framework_score(["django", "fastapi"], self.stack),
            20.0,
        )

    def test_framework_score_handles_topic_casing(self):
        self.assertEqual(
            self.score.framework_score(["React-Native", "Expo"], self.stack),
            35.0,
        )

    def test_score_repositories_filters_sorts_and_limits_ranked_output(self):
        repos = [
            {"full_name": "owner/low", "stars": 999, "pushed_at": "2026-07-05T00:00:00Z"},
            {"full_name": "owner/high-fewer-stars", "stars": 5, "pushed_at": "2026-07-05T00:00:00Z"},
            {"full_name": "owner/high-more-stars", "stars": 50, "pushed_at": "2026-07-05T00:00:00Z"},
            {"full_name": "owner/mid", "stars": 1000, "pushed_at": "2026-07-05T00:00:00Z"},
        ]
        scores = {
            "owner/low": 10.0,
            "owner/high-fewer-stars": 90.0,
            "owner/high-more-stars": 90.0,
            "owner/mid": 70.0,
        }

        with mock.patch.object(self.score, "compute_score", side_effect=lambda repo, stack: scores[repo["full_name"]]):
            ranked = self.score.score_repositories(repos, self.stack, min_score=60.0, limit=2)

        self.assertEqual([repo["full_name"] for repo in ranked], [
            "owner/high-more-stars",
            "owner/high-fewer-stars",
        ])
        self.assertEqual([repo["score"] for repo in ranked], [90.0, 90.0])
        self.assertEqual([repo["label"] for repo in ranked], ["ADOPT", "ADOPT"])


if __name__ == "__main__":
    unittest.main()
