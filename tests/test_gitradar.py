import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]

# ── Deterministic module loading ─────────────────────────────────────
#
# Both pipeline scripts are executed at import time, and the discovery
# script reads its mode/help flags straight from ``sys.argv`` while
# deriving its data paths from ``__file__``. Loading them naively
# therefore (a) lets the test runner's own argv leak into the module
# under test and (b) points the module's cache/state at the real
# checkout. ``load_module`` closes both holes: it restores argv
# unconditionally, and re-points every state path at a throwaway
# directory before returning.

STATE_PATH_NAMES = (
    "DATA_DIR",
    "CACHE_FILE",
    "OUTPUT_FILE",
    "METRICS_FILE",
    "THRESHOLDS_FILE",
)


class _ArgvGuard:
    """Restore ``sys.argv`` even if the body raises."""

    def __enter__(self):
        self._saved = list(sys.argv)
        return self

    def __exit__(self, *exc_info):
        sys.argv[:] = self._saved
        return False


def load_module(name, relative_path, argv=None, data_dir=None):
    """Import a pipeline script with controlled argv and isolated state paths.

    argv:      full argv exposed while importing (default: the script path).
    data_dir:  directory the module's state paths are re-pointed at.
               Defaults to a fresh temporary directory.
    """
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load {relative_path}")
    module = importlib.util.module_from_spec(spec)

    real_paths = {}
    with _ArgvGuard():
        sys.argv[:] = list(argv) if argv is not None else [str(ROOT / relative_path)]
        spec.loader.exec_module(module)

    target = data_dir or tempfile.mkdtemp(prefix="gitradar-test-")
    for attr in STATE_PATH_NAMES:
        if hasattr(module, attr):
            real_paths[attr] = getattr(module, attr)
            setattr(module, attr, os.path.join(target, os.path.basename(real_paths[attr])))
    setattr(module, "_isolated_data_dir", target)
    setattr(module, "_real_state_paths", real_paths)
    return module


def repo_item(full_name, **overrides):
    """Minimal GitHub search-result item."""
    item = {
        "full_name": full_name,
        "description": "a real description",
        "stargazers_count": 200,
        "forks_count": 0,
        "language": "Go",
        "topics": [],
        "created_at": "2026-01-01T00:00:00Z",
        "pushed_at": "2026-01-02T00:00:00Z",
        "open_issues_count": 0,
        "license": None,
        "html_url": f"https://github.com/{full_name}",
    }
    item.update(overrides)
    return item


class GitRadarDiscoverTests(unittest.TestCase):
    def setUp(self):
        self.discover = load_module("gitradar_discover", "scripts/gitradar-discover.py")

    def _run_collect(self, method_name, queries, search, trending=None):
        """Call collect_daily/collect_weekly with every I/O seam patched.

        Patching search alone is not enough: the collectors also consult the
        cache and both Trending helpers, so a test that mocks only search can
        still write state or make real HTTP calls.
        """
        with mock.patch.object(self.discover, "load_cache_v2", return_value=(set(), {})), \
             mock.patch.object(self.discover, "save_cache_v2") as save_cache, \
             mock.patch.object(self.discover, "scrape_all_trending", return_value=list(trending or [])), \
             mock.patch.object(self.discover, "enrich_trending_repos", side_effect=lambda repos: list(repos)), \
             mock.patch.object(self.discover, "github_search", side_effect=search) as search_mock:
            repos, extra = getattr(self.discover, method_name)(queries)
        return repos, extra, search_mock, save_cache

    def test_collect_daily_stops_after_rate_limit_sentinel(self):
        """``([], -1)`` is the rate-limit sentinel: stop issuing further queries."""
        calls = []

        def fake_search(query, sort="stars", order="desc", per_page=100, page=1):
            calls.append(query)
            return [], -1

        queries = [
            {"q": "language:python stars:>50", "sort": "stars", "order": "desc"},
            {"q": "language:typescript stars:>50", "sort": "stars", "order": "desc"},
        ]

        repos, _, _, _ = self._run_collect("collect_daily", queries, fake_search)

        self.assertEqual(repos, [])
        # The sentinel halts collection after the first query, not after one page.
        self.assertEqual(calls, ["language:python stars:>50"])

    def test_collect_weekly_stops_after_rate_limit_sentinel(self):
        """Weekly honours the same sentinel contract."""
        calls = []

        def fake_search(query, sort="stars", order="desc", per_page=100, page=1):
            calls.append(query)
            return [], -1

        queries = [
            {"q": "language:python stars:>50", "sort": "stars", "order": "desc"},
            {"q": "language:go stars:>50", "sort": "stars", "order": "desc"},
        ]

        repos, _, _, _ = self._run_collect("collect_weekly", queries, fake_search)

        self.assertEqual(repos, [])
        self.assertEqual(calls, ["language:python stars:>50"])

    def test_collect_daily_does_not_inherit_rate_limit_between_runs(self):
        """Rate-limit state is per-run: a completed collect must not stay latched."""
        queries = [{"q": "language:python stars:>50", "sort": "stars", "order": "desc"}]

        _, _, first_search, _ = self._run_collect("collect_daily", queries, lambda *a, **k: ([], -1))
        self.assertEqual(first_search.call_count, 1)

        def one_page_only(query, sort="stars", order="desc", per_page=100, page=1):
            if page == 1:
                return [repo_item("o/r")], 1
            return [], 1

        _, _, second_search, _ = self._run_collect("collect_daily", queries, one_page_only)
        # The second, independent run searches again rather than staying latched.
        self.assertEqual(second_search.call_count, 2)

    def test_collect_daily_pages_until_short_page(self):
        """Paging continues while items arrive and stops on an empty page."""
        pages = []

        def fake_search(query, sort="stars", order="desc", per_page=100, page=1):
            pages.append(page)
            if page == 1:
                return [repo_item("o/a"), repo_item("o/b")], 2
            return [], 2

        queries = [{"q": "language:python stars:>50", "sort": "stars", "order": "desc"}]
        repos, _, _, _ = self._run_collect("collect_daily", queries, fake_search)

        self.assertEqual([r["full_name"] for r in repos], ["o/a", "o/b"])
        self.assertEqual(pages, [1, 2])

    def test_collect_writes_no_state_outside_isolated_dir(self):
        """The collect path must not touch real checkout state."""
        self.assertNotEqual(
            os.path.abspath(self.discover.CACHE_FILE),
            os.path.abspath(self.discover._real_state_paths["CACHE_FILE"]),
        )
        queries = [{"q": "language:python stars:>50", "sort": "stars", "order": "desc"}]
        self._run_collect("collect_daily", queries, lambda *a, **k: ([], 0))
        self.assertFalse(os.path.exists(self.discover.CACHE_FILE))

    def test_gh_auth_token_prefers_environment_token(self):
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": " env-token \n"}, clear=False), \
             mock.patch.object(subprocess, "run") as run:
            token = self.discover.gh_auth_token()

        self.assertEqual(token, "env-token")
        run.assert_not_called()

    # NOTE (Phase 1A ledger): this test characterises current behaviour in
    # which a Trending-only record — which carries no API metadata — is
    # classified as `dead_repo` and therefore discarded. That discard is a
    # known defect, not intended behaviour; the assertion is retained here
    # only so the eventual Phase 1C repair has a witnessed before-state.
    # Do not treat a green result here as endorsement of the behaviour.
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


class GitRadarImportIsolationTests(unittest.TestCase):
    """Exercise the import-time argv hazard directly rather than assuming it."""

    def test_mode_flag_is_read_from_argv_at_import(self):
        module = load_module(
            "gitradar_discover_weekly",
            "scripts/gitradar-discover.py",
            argv=["probe", "--mode", "weekly"],
        )
        self.assertEqual(module.MODE, "weekly")

    def test_default_mode_is_daily_without_flag(self):
        module = load_module(
            "gitradar_discover_daily", "scripts/gitradar-discover.py", argv=["probe"]
        )
        self.assertEqual(module.MODE, "daily")

    def test_load_module_restores_runner_argv(self):
        before = list(sys.argv)
        load_module(
            "gitradar_discover_argv_restore",
            "scripts/gitradar-discover.py",
            argv=["probe", "--mode", "weekly"],
        )
        self.assertEqual(sys.argv, before)

    def test_load_module_restores_argv_when_import_raises(self):
        """argv must be restored when the imported module's body itself fails.

        A valid spec/loader is used so control genuinely reaches
        ``exec_module``. Failure is injected *inside* the module body, after
        the controlled argv has been installed, which is the only way to
        exercise ``_ArgvGuard.__exit__`` on the exception path. Returning
        ``None`` from ``spec_from_file_location`` (the previous version of
        this test) raises before the guard is entered, so it proved nothing.
        """
        before = list(sys.argv)
        controlled = ["probe", "--mode", "weekly"]
        observed = {}

        spec = importlib.util.spec_from_file_location(
            "gitradar_discover_raising", ROOT / "scripts/gitradar-discover.py"
        )
        self.assertIsNotNone(spec)
        assert spec is not None and spec.loader is not None
        loader = spec.loader
        real_exec = loader.exec_module

        def exec_then_fail(module):
            # Body runs under the controlled argv; capture what it saw.
            real_exec(module)
            observed["argv_during_body"] = list(sys.argv)
            observed["mode_during_body"] = getattr(module, "MODE", None)
            raise RuntimeError("injected failure inside exec_module")

        with mock.patch.object(loader, "exec_module", side_effect=exec_then_fail):
            with mock.patch.object(importlib.util, "spec_from_file_location", return_value=spec):
                with self.assertRaises(RuntimeError):
                    load_module(
                        "gitradar_discover_raising",
                        "scripts/gitradar-discover.py",
                        argv=controlled,
                    )

        # The failure was reached, and it happened under the controlled argv.
        self.assertEqual(observed.get("argv_during_body"), controlled)
        self.assertEqual(observed.get("mode_during_body"), "weekly")
        # And the caller's argv survived that failure.
        self.assertEqual(sys.argv, before)


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


if __name__ == "__main__":
    unittest.main()
