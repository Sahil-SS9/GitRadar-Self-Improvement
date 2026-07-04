# GitRadar

Automated GitHub repository discovery with self-tuning thresholds, quality scoring, and classification. Dual-mode: daily lightweight finder + weekly full re-evaluation.

## Overview

GitRadar is a two-pipeline system that discovers trending GitHub repositories, filters out noise, scores them for relevance, and classifies them into actionable labels. Designed for developers using any AI agent framework who want to monitor GitHub for:

- New tools and libraries in their stack
- Emerging trends in specific topics (MCP, agent frameworks, etc.)
- High-quality repos worth extracting concepts from
- Potential product ideas or internal tool inspiration

v4.1 adds dual-mode operation, pushed_at tracking for weekly re-evaluation, trending as a primary source, proper rate limiting, and expanded query templates.

## Features

- **Dual-Mode Pipeline**: Daily mode (dedup'd new-find scan) + Weekly mode (full cache re-evaluation)
- **pushed_at Tracking**: Cache stores last-pushed timestamps — weekly mode flags repos with new activity
- **Trending as Primary Source**: Scrapes GitHub Trending (daily + weekly pages) as a primary discovery channel
- **Smart Discovery**: Queries GitHub Search API + trending scrape with 13 query templates
- **Self-Tuning Thresholds**: Automatically adjusts star requirements based on signal quality
- **Noise Filtering**: Removes tutorial repos, awesome lists, dead repos, non-code, and spam-starred repos
- **Relevance Scoring**: Scores each repo (0-100) based on stars, recency, language/framework match, description quality, license, and topic bonus
- **Classification**: Labels each repo as ADOPT, EXTRACT, FORK/PRODUCT, PLUGIN/SKILL, INSPIRATION, or NOISE
- **Rate Limiting**: Proper GitHub Search API 30 req/min throttle — no silent failures
- **Deduplication**: Avoids processing the same repo multiple times
- **Quality Metrics**: Tracks signal-to-noise ratio over time
- **Agent Agnostic**: Outputs JSON compatible with any AI agent system

## How It Works

GitRadar runs in two modes:

### Daily Mode (default, `--mode daily`)

Runs as a lightweight new-find scan:

1. **Collection**: Queries GitHub Search API with 13 query templates (language + topic-specific), deduplicates against cache (14-day TTL). Scrapes GitHub Trending (daily + weekly pages) as a primary source alongside API queries.
2. **Rate Limiting**: Proper 30 req/min throttle via `wait_for_rate_limit()` — no ad-hoc sleeps.
3. **Filtering**: Applies rule-based noise filters (awesome lists, tutorials, dead repos, non-code, spam patterns).
4. **Self-Tuning**: Analyzes recent signal quality and adjusts threshold (tighten on high noise, ease on good signal).
5. **Output**: `data/discoveries.json` with full stats + a human-readable summary to stdout.

### Weekly Mode (`--mode weekly`)

Runs as a full cache re-evaluation:

1. **Re-scan**: Fetches repos FROM SCRATCH with fewer pages (5 vs 10) to stay within rate limits.
2. **pushed_at Comparison**: Compares each repo's current `pushed_at` against the cached `last_pushed` timestamp.
3. **Activity Detection**: Flags repos with newer pushes since the last scan — output shows `X repos re-evaluated, Y with new activity`.
4. **Cache Refresh**: Updates `last_pushed` and `last_checked` timestamps in the cache.
5. **Output**: Same format as daily but with re-evaluation stats.

### Example output

```
CONFIG: mode=weekly, star_threshold=75, queries=13
CACHE: 2112 preserved, 0 new entries
WEEKLY: re-evaluated 2112 cached, 14 with new activity, 0 new finds
FILTER: 1972 kept, 140 filtered
TUNING:
  HOLD: noise 6.6%, signal 93.4% — thresholds unchanged

GitRadar · 19/06/2026 (WEEKLY)
1972 repos re-evaluated · 14 with new activity · 0 new finds
Star threshold: 75 (self-tuned)
Top picks:
  [NEW ACTIVITY] owner/repo — ★ 142, score 81.5
  ...
```

## Quick Start

```bash
# Clone the repository
git clone https://github.com/Sahil-SS9/GitRadar-Self-Improvement.git
cd GitRadar-Self-Improvement

# Ensure you're authenticated to GitHub
gh auth login
# Or provide a token directly
export GITHUB_TOKEN=<your-token>

# Run daily discovery safely: discover → validate → score
python3 scripts/gitradar-safe-run.py

# Run weekly re-evaluation safely: discover → validate → score
python3 scripts/gitradar-safe-run.py --mode weekly

# Or use the convenience wrapper
bash scripts/gitradar-weekly.sh

# View results
cat data/discoveries.json
cat data/recommendations.json
```

### Scheduled Usage

Set up two cron jobs for best coverage. Use the safe wrapper in scheduled runs;
it fails loudly if `discoveries.json` is malformed, internally inconsistent, or
shows the known trending-metadata skeleton regression.

```cron
# Daily new-find scan
30 4 * * * cd /path/to/GitRadar && mkdir -p logs && python3 scripts/gitradar-safe-run.py >> logs/gitradar.log 2>&1

# Weekly full re-evaluation (Mondays)
0 6 * * 1 cd /path/to/GitRadar && mkdir -p logs && python3 scripts/gitradar-safe-run.py --mode weekly >> logs/gitradar.log 2>&1
```

For first-run shakedowns, add `--fail-on-empty` or `--min-repos N` to make an
unexpectedly empty collection fail instead of only warning. For mature daily runs,
zero new repos can be legitimate when the cache absorbs duplicates.

## Configuration

### Stack Preferences (`config/stack.json`)

Define your technology stack for scoring in `config/stack.json` — the scoring system uses built-in relevance keywords by default. Customise per your stack:

- Language weights (Python=30, TypeScript=30, etc.)
- Framework weights (React Native=35, Expo=30, etc.)
- Ecosystem keywords (MCP, agent-framework, etc.)
- License preferences
- Noise description keywords

The default config ships with a generic profile tuned for agent/tooling developers.

### Query Templates (in `scripts/gitradar-discover.py`)

13 query templates covering:

- **Primary**: `stars:>{threshold}`
- **Languages**: Python, TypeScript, Go, Rust
- **Topics**: MCP, agent-framework, developer-tools, hermes-plugin
- **Stack-specific**: React Native, Flutter, voice-assistant, sports-prediction

Customise by editing `QUERY_TEMPLATES` in the script. Topic queries use a low star floor (5-20) by design to catch emerging repos.

### Tuning Parameters (auto-managed in `data/thresholds.json`)

- Star threshold (auto-adjusted based on signal quality)
- Noise/signal thresholds for tuning decisions
- Consecutive run requirements for adjustments

Default threshold: 75 (self-tunes between 25-500).

## Output Format

### Discoveries JSON (`data/discoveries.json`)

```json
{
  "collected_at": "2026-06-19T14:06:08Z",
  "mode": "weekly",
  "stats": {
    "total_collected": 2112,
    "after_filter": 1972,
    "after_dedup": 1972,
    "scored": 1972,
    "noise": 140,
    "noise_rate_pct": 6.6,
    "signal_rate_pct": 93.4,
    "active_threshold": 75,
    "tuning_actions": ["HOLD: noise 6.6%, signal 93.4% — thresholds unchanged"],
    "collection_queries": 13
  },
  "filter_reasons": {
    "awesome_list": 3,
    "non_code": 18,
    "name_noise": 1,
    "spam_name": 12,
    "dead_repo": 106
  },
  "tuning": {
    "actions": ["..."],
    "thresholds": {
      "star_threshold": 75,
      "noise_keywords_count": 8,
      "language_filters": ["HTML", "CSS", "Markdown"]
    }
  },
  "extra_stats": {
    "re_evaluated": 2112,
    "with_new_activity": 14,
    "new_finds": 0
  },
  "repos": [...]
}
```

Note: `stats` always present. `extra_stats` shape depends on mode:
- **Daily**: `{total, new_api, new_trending, refreshed}`
- **Weekly**: `{re_evaluated, with_new_activity, new_finds, total_in_scope}`

### Per-repo entry

```json
{
  "full_name": "owner/repo-name",
  "description": "Repo description",
  "stars": 420,
  "forks": 0,
  "language": "Python",
  "topics": ["topic1", "topic2"],
  "created_at": "2026-05-14T21:29:20Z",
  "pushed_at": "2026-05-14T21:29:26Z",
  "open_issues": 0,
  "license": "MIT",
  "html_url": "https://github.com/owner/repo-name",
  "source": "api",
  "score": 68.3,
  "classification": "EXTRACT",
  "why": "On-mission repo: worth extracting concepts or patterns."
}
```

### Cron-safe validation

`scripts/gitradar-validate.py` checks `data/discoveries.json` before downstream
agents consume it:

```bash
python3 scripts/gitradar-validate.py --input data/discoveries.json
```

It validates:

- required top-level fields and mode-specific `extra_stats`
- stats consistency (`after_filter + noise == total_collected`, `len(repos) == after_dedup`)
- required repo fields and numeric types
- duplicate repos
- enriched trending repos collapsing back to zero-star skeletons

The safe runner combines discovery, validation, and scoring:

```bash
python3 scripts/gitradar-safe-run.py --mode daily
python3 scripts/gitradar-safe-run.py --mode weekly --fail-on-empty
```

## Integration with AI Agent Systems

GitRadar works with any AI agent framework that can consume JSON and execute cron jobs:

### Hermes Agent / Other Frameworks

1. Copy `scripts/gitradar-discover.py`, `scripts/gitradar-validate.py`, `scripts/gitradar-score.py`, and `scripts/gitradar-safe-run.py` to your scripts directory
2. Configure a cron job running the safe wrapper daily (`--mode daily`) + weekly (`--mode weekly`)
3. Consume `data/discoveries.json` and/or `data/recommendations.json` only after validation succeeds

The `mode` field in the output tells you whether it was a daily or weekly run, so downstream processors can interpret `extra_stats` accordingly.

## Self-Tuning Explained

GitRadar learns from its own performance to keep the signal clean:

- **Noise > 40% for 3 consecutive runs** → Star threshold increases by 25 (tighten)
- **Signal > 60% AND Noise < 20% for 3 consecutive runs** → Star threshold decreases by 25 (ease)
- **Signal < 10% for 5 consecutive runs** → Aggressive increase (threshold +50)
- **Otherwise** → No change (hold)

All tuning decisions are logged in the discovery output and saved to `thresholds.json` for inspection.

Default threshold: 75. Range: 25-500.

## Requirements

- Python 3.8+
- GitHub CLI (`gh`) authenticated with `repo` scope, or a `GITHUB_TOKEN`/`GH_TOKEN` environment variable
- Internet access to GitHub API + github.com (for trending scrape)

No Python packages required — uses only standard library.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## v4.1 Changelog

- **Dual-mode**: `--mode daily` (default) + `--mode weekly` (full re-evaluation)
- **pushed_at tracking**: Cache stores first_seen, last_pushed, last_checked per repo
- **Weekly re-evaluation**: Compares current pushed_at against cached version — detects repos with fresh activity
- **Trending as primary source**: Scrapes both daily AND weekly trending pages
- **4 new query templates**: react-native, flutter, voice-assistant, sports-prediction
- **Rate limiting**: Proper GitHub Search API 30 req/min throttle with timestamp tracking
- **Lowered default threshold**: star_threshold = 75 (was 100) based on 93%+ signal rates
- **Cache stats**: Output shows cache totals even when 0 new repos found
- **Better output**: Mode-aware human-readable summary to stdout

## License

MIT License — feel free to fork, modify, and deploy.

## Contributing

1. Fork the repository
2. Create a feature branch
3. Commit your changes
4. Push to the branch
5. Open a pull request

Please ensure any changes maintain the deterministic nature of the discovery script and keep it self-contained (stdlib only).

## Related Projects

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) — The AI agent framework that originally used GitRadar
