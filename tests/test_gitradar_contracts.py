"""Contract regressions for the R1-R7 correction batch (public variant).

Each test pins a fault the independent review reproduced, so the corrected
behaviour is asserted rather than merely described.
"""
import json
import os
import pathlib
import tempfile
import threading
import unittest
from unittest import mock

from test_gitradar import load_module


def repo(name, stars=2000, description="agent tooling mcp server", **over):
    row = {
        "full_name": name, "description": description, "stars": stars, "forks": 1,
        "language": "Python", "topics": ["llm", "mcp"],
        "created_at": "2026-11-01T00:00:00Z", "pushed_at": "2026-11-20T00:00:00Z",
        "open_issues": 0, "license": "MIT", "html_url": f"https://github.com/{name}",
        "source": "api", "metadata_status": "complete",
    }
    row.update(over)
    return row


class R1UnknownMetadata(unittest.TestCase):
    """R1: unknown metadata must survive scoring and render as unknown."""

    def setUp(self):
        self.d = load_module("r1_public", "scripts/gitradar-discover.py")

    def test_unknown_stars_does_not_crash_scoring(self):
        row = repo("acme/unknown", stars=None, metadata_status="unavailable",
                   language="", topics=[])
        scored = self.d.score_all([row])[0]
        self.assertIn("score", scored)
        self.assertIn("classification", scored)

    def test_unknown_renders_as_unknown_not_zero(self):
        rows = [repo("acme/unknown", stars=None, metadata_status="unavailable",
                     language="", topics=[])]
        for r in rows:
            r["score"] = 12.0
            r["classification"] = "INSPIRATION"
            r["why"] = "x"
        text = self.d.build_text_summary(rows, {
            "active_threshold": 75, "collection_queries": 1, "total_collected": 1,
            "after_filter": 1, "after_dedup": 1, "scored": 1, "noise": 0,
            "signal_rate_pct": 100.0, "noise_rate_pct": 0.0, "tuning_actions": [],
        }, {})
        self.assertNotIn("★None", text)
        self.assertIn("unknown", text.lower())

    def test_measured_zero_still_dead(self):
        row = repo("acme/dead", stars=0, description="", topics=[], language="")
        self.assertEqual(self.d.classify_noise(row, self.d.DEFAULT_THRESHOLDS), (True, "dead_repo"))

    def test_successful_enrichment_still_preserved(self):
        row = repo("acme/rich", stars=480, description="real", language="TypeScript",
                   topics=["llm"], metadata_status="complete")
        self.assertEqual(row["stars"], 480)
        self.assertTrue(self.d.metadata_is_known(row))


class R2CompleteGeneration(unittest.TestCase):
    """R2: a failed/partial run must not masquerade as a completed one."""

    def setUp(self):
        self.d = load_module("r2_public", "scripts/gitradar-discover.py")

    def test_partial_run_does_not_overwrite_last_good(self):
        self.d.reset_acquisition()
        self.d.record_acquisition(self.d.ACQ_TIMEOUT, "boom")
        self.assertTrue(self.d.acquisition_status()["partial"])

    def test_aggregate_status_exposed(self):
        self.d.reset_acquisition()
        self.d.record_acquisition(self.d.ACQ_OK)
        self.d.record_acquisition(self.d.ACQ_FORBIDDEN, "403")
        st = self.d.acquisition_status()
        self.assertEqual(st["worst"], self.d.ACQ_FORBIDDEN)
        self.assertTrue(st["partial"])
        self.assertEqual(st["calls"], 2)

    def test_partial_run_does_not_replace_published_discoveries(self):
        """A failed scan must not overwrite the last good shared output.

        Driven through the real main() rather than re-implementing its
        publish decision, so the guard is actually exercised.
        """
        out = pathlib.Path(self.d.OUTPUT_FILE)
        out.write_text('{"previous": "good run"}', encoding="utf-8")

        with mock.patch.object(self.d, "gh_auth_token", return_value=""), \
             mock.patch.object(self.d, "wait_for_rate_limit"), \
             mock.patch.object(self.d, "scrape_all_trending", return_value=[]), \
             mock.patch.object(self.d, "enrich_trending_repos", side_effect=lambda r: list(r)), \
             mock.patch("sys.stdout", new_callable=lambda: __import__("io").StringIO()), \
             mock.patch("sys.stderr", new_callable=lambda: __import__("io").StringIO()) as err:
            self.d.main()
            stderr = err.getvalue()

        self.assertEqual(out.read_text(encoding="utf-8"), '{"previous": "good run"}',
                         "a failed run overwrote the last good output")

        # A failed attempt writes diagnostics beside the committed generation
        # and leaves the commit pointer untouched.
        self.assertTrue(pathlib.Path(self.d.ATTEMPT_FILE).exists(),
                        "the attempt record was not written")
        attempt = json.loads(pathlib.Path(self.d.ATTEMPT_FILE).read_text())
        self.assertEqual(attempt["status"], "partial")
        self.assertFalse(pathlib.Path(self.d.COMMIT_FILE).exists(),
                         "a failed first publication must not commit")
        self.assertIn("kept as last-good", stderr)

    def test_complete_run_publishes(self):
        """A clean run still publishes as before."""
        out = pathlib.Path(self.d.OUTPUT_FILE)
        out.write_text('{"previous": "good run"}', encoding="utf-8")
        with mock.patch.object(self.d, "gh_auth_token", return_value="tok"), \
             mock.patch.object(self.d, "wait_for_rate_limit"), \
             mock.patch.object(self.d, "scrape_all_trending", return_value=[]), \
             mock.patch.object(self.d, "enrich_trending_repos", side_effect=lambda r: list(r)), \
             mock.patch.object(self.d, "github_search", return_value=([], 0)), \
             mock.patch("sys.stdout", new_callable=lambda: __import__("io").StringIO()), \
             mock.patch("sys.stderr", new_callable=lambda: __import__("io").StringIO()):
            self.d.main()
        published = json.loads(out.read_text(encoding="utf-8"))
        self.assertIn("repos", published)
        manifest = json.loads(pathlib.Path(self.d.MANIFEST_FILE).read_text())
        self.assertEqual(manifest["status"], "complete")
        self.assertTrue(manifest["tuning_eligible"])
        commit = json.loads(pathlib.Path(self.d.COMMIT_FILE).read_text())
        self.assertEqual(commit["generation"], manifest["generation"])


    def test_reader_rejects_a_tampered_generation(self):
        """The committed result is bound to bytes, not just to a filename."""
        with mock.patch.object(self.d, "gh_auth_token", return_value="tok"), \
             mock.patch.object(self.d, "wait_for_rate_limit"), \
             mock.patch.object(self.d, "scrape_all_trending", return_value=[]), \
             mock.patch.object(self.d, "enrich_trending_repos", side_effect=lambda r: list(r)), \
             mock.patch.object(self.d, "github_search", return_value=([], 0)), \
             mock.patch("sys.stdout", new_callable=lambda: __import__("io").StringIO()), \
             mock.patch("sys.stderr", new_callable=lambda: __import__("io").StringIO()):
            self.d.main()

        committed = self.d.read_committed_discoveries()
        self.assertIsNotNone(committed, "a clean run did not commit")
        payload, commit = committed
        self.assertEqual(commit["status"], "complete")

        # Same block count, different bytes: must not be accepted.
        pathlib.Path(self.d.OUTPUT_FILE).write_text('{"repos": [], "tampered": true}')
        self.assertIsNone(self.d.read_committed_discoveries(),
                          "tampered bytes were accepted as the committed generation")

    def test_reader_fails_closed_without_a_commit_marker(self):
        pathlib.Path(self.d.OUTPUT_FILE).write_text('{"repos": []}')
        if pathlib.Path(self.d.COMMIT_FILE).exists():
            pathlib.Path(self.d.COMMIT_FILE).unlink()
        self.assertIsNone(self.d.read_committed_discoveries())


class R3Transactions(unittest.TestCase):
    """R3: the threshold decision is one transaction."""

    def setUp(self):
        self.d = load_module("r3_public", "scripts/gitradar-discover.py")

    def test_threshold_transaction_keeps_both_histories(self):
        self.d.save_thresholds(dict(self.d.DEFAULT_THRESHOLDS, star_threshold=100))
        prior = [{"noise_rate_pct": 5.0, "signal_rate_pct": 95.0}] * 4
        with mock.patch.object(self.d, "load_metrics", return_value=list(prior)):
            a, acts_a, _ = self.d.tune_and_publish(5.0, 95.0, self.d.ACQ_OK)
            b, acts_b, _ = self.d.tune_and_publish(5.0, 95.0, self.d.ACQ_OK)
        written = json.loads(pathlib.Path(self.d.THRESHOLDS_FILE).read_text())
        self.assertEqual(len(written.get("history", [])), 2, "a history event was lost")
        self.assertEqual(written["star_threshold"], b["star_threshold"])

    def test_lock_is_reentrant_without_deadlock(self):
        with self.d.StateLock():
            with self.d.StateLock():
                inside = True
        self.assertTrue(inside)

    def test_stale_cache_save_keeps_concurrent_names(self):
        self.d.save_cache_v2({"alpha"}, {})
        self.d.save_cache_v2({"alpha", "beta"}, {})
        self.d.save_cache_v2({"alpha"}, {})          # stale writer
        seen = json.loads(pathlib.Path(self.d.CACHE_FILE).read_text())["seen"]
        self.assertIn("beta", seen)

    def test_checked_set_does_not_refresh_unchecked_last_checked(self):
        self.d.save_cache_v2({"alpha", "quiet"}, {"alpha": "2026-11-20T00:00:00Z"},
                             checked_set={"alpha"})
        first = json.loads(pathlib.Path(self.d.CACHE_FILE).read_text())["seen"]
        quiet_ts = first["quiet"]["last_checked"]
        self.d.save_cache_v2({"alpha", "quiet"}, {}, checked_set={"alpha"})
        second = json.loads(pathlib.Path(self.d.CACHE_FILE).read_text())["seen"]
        self.assertEqual(second["quiet"]["last_checked"], quiet_ts,
                         "an unchecked repo was stamped as freshly checked")


class R3CommitHonesty(unittest.TestCase):
    """R3: a failed threshold commit must not be reported as success."""

    def setUp(self):
        self.d = load_module("r3_commit", "scripts/gitradar-discover.py")

    def test_failed_commit_returns_committed_values(self):
        self.d.save_thresholds(dict(self.d.DEFAULT_THRESHOLDS, star_threshold=125))
        prior = [{"noise_rate_pct": 5.0, "signal_rate_pct": 95.0}] * 4
        real = self.d.atomic_write_json

        def explode(path, data, indent=2):
            if path == self.d.THRESHOLDS_FILE:
                raise OSError("injected thresholds write failure")
            return real(path, data, indent=indent)

        with mock.patch.object(self.d, "load_metrics", return_value=list(prior)), \
             mock.patch.object(self.d, "atomic_write_json", side_effect=explode):
            updated, actions, committed = self.d.tune_and_publish(5.0, 95.0, self.d.ACQ_OK)

        self.assertFalse(committed, "a failed commit was reported as success")
        self.assertEqual(updated["star_threshold"], 125,
                         "returned a threshold that never landed")
        self.assertTrue(any("COMMIT FAILED" in a for a in actions), actions)
        on_disk = json.loads(pathlib.Path(self.d.THRESHOLDS_FILE).read_text())
        self.assertEqual(on_disk["star_threshold"], 125)

    def test_successful_commit_matches_disk(self):
        self.d.save_thresholds(dict(self.d.DEFAULT_THRESHOLDS, star_threshold=125))
        prior = [{"noise_rate_pct": 5.0, "signal_rate_pct": 95.0}] * 4
        with mock.patch.object(self.d, "load_metrics", return_value=list(prior)):
            updated, actions, committed = self.d.tune_and_publish(5.0, 95.0, self.d.ACQ_OK)
        self.assertTrue(committed)
        on_disk = json.loads(pathlib.Path(self.d.THRESHOLDS_FILE).read_text())
        self.assertEqual(on_disk["star_threshold"], updated["star_threshold"])


class R4Acquisition(unittest.TestCase):
    """R4: a late success cannot erase an earlier failure."""

    def setUp(self):
        self.d = load_module("r4_public", "scripts/gitradar-discover.py")

    def test_timeout_then_success_is_partial(self):
        self.d.reset_acquisition()
        self.d.record_acquisition(self.d.ACQ_TIMEOUT, "q1")
        self.d.record_acquisition(self.d.ACQ_OK)
        self.d.record_acquisition(self.d.ACQ_OK)
        st = self.d.acquisition_status()
        self.assertEqual(st["worst"], self.d.ACQ_TIMEOUT)
        self.assertTrue(st["partial"], "later success erased an earlier failure")

    def test_partial_scan_is_not_a_tuning_observation(self):
        self.d.save_thresholds(dict(self.d.DEFAULT_THRESHOLDS, star_threshold=75))
        prior = [{"noise_rate_pct": 0.0, "signal_rate_pct": 0.0}] * 5
        with mock.patch.object(self.d, "load_metrics", return_value=list(prior)):
            updated, actions, committed = self.d.tune_and_publish(0.0, 0.0, self.d.ACQ_TIMEOUT)
        self.assertEqual(updated["star_threshold"], 75)
        self.assertTrue(actions[0].startswith("SKIP"), actions)

    def test_public_plus_50_branch_preserved(self):
        self.d.save_thresholds(dict(self.d.DEFAULT_THRESHOLDS, star_threshold=75))
        prior = [{"noise_rate_pct": 0.0, "signal_rate_pct": 0.0}] * 5
        with mock.patch.object(self.d, "load_metrics", return_value=list(prior)):
            updated, actions, committed = self.d.tune_and_publish(0.0, 0.0, self.d.ACQ_OK)
        self.assertEqual(updated["star_threshold"], 125, actions)


class R4PersistedHistory(unittest.TestCase):
    """R4: failed runs must not leak through stored history."""

    def setUp(self):
        self.d = load_module("r4_history", "scripts/gitradar-discover.py")

    def test_ineligible_rows_are_excluded_from_the_window(self):
        self.d.save_thresholds(dict(self.d.DEFAULT_THRESHOLDS, star_threshold=75))
        genuine = [{"noise_rate_pct": 5.0, "signal_rate_pct": 95.0,
                    "observation_eligible": True}] * 4
        failed = [{"noise_rate_pct": 100.0, "signal_rate_pct": 0.0,
                   "observation_eligible": False, "status": "partial"}]
        with mock.patch.object(self.d, "load_metrics", return_value=list(failed + genuine)):
            updated, actions, committed = self.d.tune_and_publish(5.0, 95.0, self.d.ACQ_OK)
        self.assertFalse(any(a.startswith("RAISE") for a in actions), actions)

    def test_legacy_rows_without_marker_stay_eligible(self):
        self.d.save_thresholds(dict(self.d.DEFAULT_THRESHOLDS, star_threshold=75))
        legacy = [{"noise_rate_pct": 0.0, "signal_rate_pct": 0.0}] * 5
        with mock.patch.object(self.d, "load_metrics", return_value=list(legacy)):
            updated, actions, _ = self.d.tune_and_publish(0.0, 0.0, self.d.ACQ_OK)
        self.assertEqual(updated["star_threshold"], 125,
                         "legacy history was wrongly excluded")

    def test_reset_happens_on_every_main_invocation(self):
        """A previous in-process failure must not poison the next run."""
        self.d.record_acquisition(self.d.ACQ_TIMEOUT, "earlier run")
        self.assertTrue(self.d.acquisition_status()["partial"])
        with mock.patch.object(self.d, "gh_auth_token", return_value="tok"), \
             mock.patch.object(self.d, "wait_for_rate_limit"), \
             mock.patch.object(self.d, "scrape_all_trending", return_value=[]), \
             mock.patch.object(self.d, "enrich_trending_repos", side_effect=lambda r: list(r)), \
             mock.patch.object(self.d, "github_search", return_value=([], 0)), \
             mock.patch("sys.stdout", new_callable=lambda: __import__("io").StringIO()), \
             mock.patch("sys.stderr", new_callable=lambda: __import__("io").StringIO()):
            self.d.main()
        self.assertEqual(self.d.acquisition_status()["worst"], self.d.ACQ_OK,
                         "acquisition state was not reset at the start of main")


class R5Coverage(unittest.TestCase):
    """R5: weekly history, honest coverage and the bounded recheck."""

    def setUp(self):
        self.d = load_module("r5_public", "scripts/gitradar-discover.py")

    def _collect(self, cache, search):
        with mock.patch.object(self.d, "load_cache_v2", return_value=(set(cache), {})), \
             mock.patch.object(self.d, "save_cache_v2") as save, \
             mock.patch.object(self.d, "scrape_all_trending", return_value=[]), \
             mock.patch.object(self.d, "enrich_trending_repos", side_effect=lambda r: list(r)), \
             mock.patch.object(self.d, "github_search", side_effect=search):
            repos, extra = self.d.collect_weekly([{"q": "q", "sort": "stars", "order": "desc"}])
        return repos, extra, save

    def test_empty_weekly_keeps_history_and_reports_zero_checked(self):
        def search(q, sort="stars", order="desc", per_page=100, page=1):
            return [], 0
        repos, extra, save = self._collect({"a", "b", "c"}, search)
        self.assertEqual(repos, [])
        self.assertEqual(extra["checked"], 0, "claimed checks that never happened")
        self.assertEqual(extra["cache_size"], 3)
        saved = save.call_args[0][0]
        self.assertEqual(saved, {"a", "b", "c"}, "weekly deleted the watchlist")

    def test_recheck_disabled_without_budget(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GITHUB_RADAR_RECHECK_BUDGET", None)
            out = self.d.recheck_tracked()
        self.assertFalse(out["enabled"])
        self.assertIn("no per-run recheck budget", out["reason"])
        self.assertEqual(out["checked"], 0)

    def test_recheck_is_bounded_and_resumes(self):
        calls = []

        def fetch(ident):
            calls.append(ident)
            return {"pushed_at": "2026-11-21T00:00:00Z"}

        self.d.atomic_write_json(self.d.CACHE_FILE, {"seen": {
            f"acme/r{i}": {"first_seen": "2026-11-20T00:00:00Z",
                          "last_pushed": "2026-11-01T00:00:00Z",
                          "last_checked": ""} for i in range(5)
        }})
        first = self.d.recheck_tracked(budget=2, fetch=fetch)
        self.assertEqual(first["checked"], 2)
        self.assertEqual(len(calls), 2, "budget exceeded")
        second = self.d.recheck_tracked(budget=2, fetch=fetch)
        self.assertEqual(second["checked"], 2)
        # Resumed rather than restarting from the same first two.
        self.assertEqual(len(calls), 4)
        self.assertNotEqual(calls[0], calls[2], "recheck restarted instead of resuming")
        self.assertGreater(second["deferred"], 0, "backlog not reported")


class R7Outcomes(unittest.TestCase):
    """R7: minimal outcome contract with honest labels."""

    def setUp(self):
        self.d = load_module("r7_public", "scripts/gitradar-discover.py")

    def test_states_are_distinct_and_recorded(self):
        for state in self.d.OUTCOME_STATES:
            self.d.record_outcome("acme/alpha", state)
        rows = self.d.read_outcomes("acme/alpha")
        self.assertEqual({r["state"] for r in rows}, set(self.d.OUTCOME_STATES))

    def test_duplicate_events_are_deduplicated(self):
        self.d.record_outcome("acme/alpha", "useful")
        self.d.record_outcome("acme/alpha", "useful")
        rows = self.d.read_outcomes("acme/alpha", state="useful")
        self.assertEqual(len(rows), 1, "duplicate event stored")
        self.assertEqual(rows[0]["count"], 2)

    def test_identity_is_stable_across_casing(self):
        self.d.record_outcome("Acme/Alpha", "investigate")
        self.assertEqual(len(self.d.read_outcomes("acme/alpha")), 1)

    def test_unknown_state_rejected(self):
        with self.assertRaises(ValueError):
            self.d.record_outcome("acme/alpha", "definitely-not-a-state")

    def test_no_response_is_not_rejection(self):
        summary = self.d.outcome_summary()
        self.assertIn("no_outcome_recorded", summary)
        self.assertIsNone(summary["no_outcome_recorded"])
        self.assertEqual(summary["irrelevant"], 0)

    def test_filter_acceptance_labelled_honestly(self):
        note = self.d.METRIC_LABEL_NOTE
        self.assertIn("not a measure of usefulness", note)


if __name__ == "__main__":
    unittest.main()
