# GitRadar

Automated GitHub repository discovery with self-tuning thresholds, quality scoring, and classification.

## Overview

GitRadar is a two-step pipeline that discovers trending GitHub repositories, filters out noise, scores them for relevance, and classifies them into actionable labels. Originally built for the Hermes Agent System, this community edition makes it available to developers using any AI agent framework who want to monitor GitHub for:

- New tools and libraries in their stack
- Emerging trends in specific topics (MCP, agent frameworks, etc.)
- High-quality repos worth extracting concepts from
- Potential product ideas or internal tool inspiration

## Features

- **Smart Discovery**: Queries GitHub Search API + scrapes trending page
- **Self-Tuning Thresholds**: Automatically adjusts star requirements based on signal quality
- **Noise Filtering**: Removes tutorial repos, awesome lists, dead repos, and non-code
- **Relevance Scoring**: Scores each repo (0-100) based on stars, recency, language/framework match, description quality, license, and topic bonus
- **Classification**: Labels each repo as ADOPT, EXTRACT, FORK/PRODUCT, PLUGIN/SKILL, INSPIRATION, or NOISE
- **Deduplication**: Avoids processing the same repo multiple times
- **Quality Metrics**: Tracks signal-to-noise ratio over time
- **Agent Agnostic**: Outputs JSON compatible with Hermes Agent, OpenClaw, Claude Code, and any other AI agent system

## How It Works

GitRadar runs in two stages:

### Stage 1: Discovery (`gitradar-discover.py`)
1. **Collection**: Queries GitHub API for repos with recent activity (pushed in last 7 days, created within 90 days), supplements with trending scrape
2. **Filtering**: Applies rule-based noise filters (awesome lists, tutorials, dead repos, non-code)
3. **Self-Tuning**: After each run, analyzes recent signal quality and adjusts thresholds:
   - If noise is consistently high → increases minimum star requirement
   - If signal is consistently good → decreases minimum star requirement to catch more
   - Output includes tuning decisions for transparency
4. **Output**: Saves `data/discoveries.json` and prints JSON to stdout (capped at 200 repos)

### Stage 2: Scoring & Classification (`gitradar-score.py`)
1. **Input**: Reads `data/discoveries.json` from the discovery step
2. **Scoring**: Each repo gets a relevance score (0-100) based on:
   - Stars (log scale, max 20 points)
   - Recency (newer = more points, max 15 points)
   - Language match (your stack preferences, max 30 points)
   - Framework match (your stack preferences, max 30 points)
   - Description quality (non-empty, no noise keywords, max 10 points)
   - License (MIT=10, Apache=9, etc., max 10 points)
   - Topic bonus (ecosystem keywords like MCP, agent-framework, +10 points)
3. **Classification**: Based on score thresholds:
   - **ADOPT** (≥80): Install and use internally
   - **EXTRACT** (≥60): Steal concepts/architecture
   - **FORK/PRODUCT** (≥50): Viable product foundation
   - **PLUGIN/SKILL** (≥40): Build for agent ecosystem
   - **INSPIRATION** (≥20): File for future reference
   - **NOISE** (<20): Drop
4. **Output**: Saves `data/recommendations.json` with score and label per repo

## Quick Start

```bash
# Clone the repository
git clone https://github.com/yourusername/gitradar.git
cd gitradar

# Ensure you're authenticated to GitHub
gh auth login
# Or provide a token directly
export GITHUB_TOKEN=<your-token>

# Run discovery (collects and filters repos)
python3 scripts/gitradar-discover.py

# Run scoring (adds relevance scores and classifications)
python3 scripts/gitradar-score.py

# View results
cat data/recommendations.json
```

## Configuration

### Stack Preferences (`config/stack.json`)
Define your technology stack for scoring:
- Language weights (Python=30, TypeScript=30, etc.)
- Framework weights (React Native=35, Expo=30, etc.)
- Ecosystem keywords (MCP, agent-framework, etc.)
- License preferences (MIT preferred, etc.)
- Noise description keywords (to detect sketchy repos)

Scoring is customised per-user by editing `config/stack.json`. The default config is generic — tune the weights to your actual stack for relevant results.

**Future enhancement**: A helper script (`scripts/generate-stack.py`) that scans local `package.json`, `requirements.txt`, `Cargo.toml`, etc. and analyses git commit history for language/framework usage to generate a suggested `stack.json` as a starting point — leaving final tuning to you.

### Tuning Parameters (auto-managed in `data/thresholds.json`)
- Star threshold (auto-adjusted based on signal quality)
- Noise/signal thresholds for tuning decisions
- Consecutive run requirements for adjustments

## Output Format

### Discoveries JSON (`data/discoveries.json`)
Raw output from discovery step:
```json
{
  "collected_at": "2026-05-16T20:28:12.583313Z",
  "stats": {
    "total_collected": 208,
    "after_filter": 174,
    "after_dedup": 174,
    "collection_queries": 9,
    "noise_rate_pct": 16.3,
    "signal_rate_pct": 83.7,
    "active_threshold": 100
  },
  "filter_reasons": {
    "awesome_list": 3,
    "dead_repo": 18,
    "name_noise": 1,
    "non_code": 12
  },
  "tuning": {
    "actions": ["Not enough data to tune (need 2+ runs)"],
    "thresholds": {
      "star_threshold": 100,
      "noise_keywords_count": 8,
      "language_filters": ["HTML", "CSS", "Markdown"]
    }
  },
  "repos": [
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
      "source": "api"
    }
  ]
}
```

### Recommendations JSON (`data/recommendations.json`)
Enriched output from scoring step:
```json
{
  "collected_at": "2026-05-16T20:32:24.670489Z",
  "total_repos": 174,
  "repos": [
    {
      "full_name": "FULU-Foundation/OrcaSlicer-bambulab",
      "description": "",
      "stars": 5224,
      "forks": 2940,
      "language": "C++",
      "topics": [],
      "created_at": "2026-05-11T17:44:55Z",
      "pushed_at": "2026-05-12T18:58:00Z",
      "open_issues": 23,
      "license": "AGPL-3.0",
      "html_url": "https://github.com/FULU-Foundation/OrcaSlicer-bambulab",
      "source": "api",
      "score": 35.59,
      "label": "INSPIRATION"
    }
  ]
}
```

## Integration with AI Agent Systems

GitRadar works with any AI agent framework that can consume JSON and execute cron jobs:

### Hermes Agent
1. Copy `scripts/gitradar-discover.py` to `~/.hermes/scripts/`
2. Copy `scripts/gitradar-score.py` to `~/.hermes/scripts/`
3. Configure a cron job that runs discovery then scoring
4. The `code-discovery-pipeline` skill can consume the recommendations

### OpenClaw / Claude Code / Any Framework
1. Run the two scripts as part of your workflow
2. Consume the `recommendations.json` output
3. Use the labels and scores to prioritize actions:
   - **ADOPT**: Install and evaluate for internal use
   - **EXTRACT**: Study the architecture/algorithms for ideas
   - **FORK/PRODUCT**: Assess as potential product foundation
   - **PLUGIN/SKILL**: Consider building a skill/plugin from it
   - **INSPIRATION**: Keep in watchlist for future evaluation
   - **NOISE**: Ignore

## Self-Tuning Explained

GitRadar learns from its own performance to keep the signal clean:

- **Noise > 40% for 3 consecutive runs** → Star threshold increases by 25
- **Signal > 60% AND Noise < 20% for 3 consecutive runs** → Star threshold decreases by 25
- **Signal < 10% for 5 consecutive runs** → Aggressive increase (threshold +50)

All tuning decisions are logged in the discovery output and saved to `thresholds.json` for inspection.

## Requirements

- Python 3.8+
- GitHub CLI (`gh`) authenticated with `repo` scope, or a `GITHUB_TOKEN`/`GH_TOKEN` environment variable
- Internet access to GitHub API

No Python packages required — uses only standard library.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## License

MIT License — feel free to fork, modify, and deploy.

## Contributing

1. Fork the repository
2. Create a feature branch
3. Commit your changes
4. Push to the branch
5. Open a pull request

Please ensure any changes maintain the deterministic nature of the discovery script.

## Related Projects

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) — The AI agent framework that originally used GitRadar
