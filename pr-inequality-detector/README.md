# PR Inequality Detector

> AI-powered Pull Request Fairness & Review Intelligence Platform
> Built for **Mind AIthon 2026**

Analyze any GitHub repository's pull request activity to surface review inequality, delay bottlenecks, low-quality approvals, risky PRs, favoritism patterns, and reviewer burnout — all in a single dashboard with an AI chat assistant grounded in your team's actual data.

---

## Features

All nine features are fully implemented — no mocks, no placeholders, all powered by real GitHub data.

1. **Reviewer Inequality Detection** — Gini coefficient, fairness score, distribution chart, overloaded reviewers (top percentile + load multiplier), inactive reviewers (>30 days dormant or requested-but-never-reviewed).
2. **PR Delay Analytics** — first-review time histogram, approval turnaround, time-to-merge (avg/median/p90), stale PR detection (open >7d or open without review >3d).
3. **AI Review Quality Analysis** — every comment classified into rubber-stamp / shallow / moderate / constructive / detailed / high-quality, plus per-reviewer quality rankings, rubber-stamp approval table, and AI-graded sample comments.
4. **PR Risk Scoring** — 0–100 score per PR combining diff size, single-reviewer coverage, fast approvals (<1h on >100-line diffs), comment count, file spread, and review presence. Bucketed into high / medium / low.
5. **Bias / Favoritism Detection** — surfaces reviewer-author pairs with ≥60% approval rate, repeated approval groups, and isolated authors who only ever get one reviewer.
6. **Smart Reviewer Recommendation** — for every open PR, suggests three balanced reviewers ranked by recent load and overall workload.
7. **Burnout Detection** — late-night (22:00–06:00) and weekend review activity per reviewer, composite burnout score, at-risk roster.
8. **AI Insights Dashboard** — composite team-health score, KPI grid, six interactive Chart.js visualizations (reviewer distribution, first-review histogram, comment quality doughnut, risk doughnut, burnout grouped bars).
9. **AI Chat Assistant** — grounded chat over the analysis JSON. Works with OpenAI, Ollama, or a built-in rule-based fallback so it always answers — even with no API key.

---

## Tech Stack

| Layer        | Tools                                  |
| ------------ | -------------------------------------- |
| Frontend     | HTML, CSS, vanilla JavaScript, Chart.js |
| Backend      | Python 3.9+, Flask, Flask-CORS         |
| Database     | SQLite (job state, chat history)       |
| Analytics    | Pandas, pure Python                    |
| AI           | OpenAI API · Ollama · rule-based fallback |
| External API | GitHub REST API v3                     |

---

## Project Structure

```
pr-inequality-detector/
├── backend/
│   ├── app.py              # Flask app, routes, background job runner, SQLite
│   ├── analytics.py        # All 9 analytics modules (Pandas + pure Python)
│   ├── ai.py               # OpenAI / Ollama / rule-based chat + review grading
│   ├── github_api.py       # Rate-limit-aware GitHub REST client
│   └── database.db         # Auto-created on first run
├── frontend/
│   ├── index.html          # Single-page dashboard
│   ├── style.css           # Editorial dark theme, Fraunces + Inter Tight
│   └── script.js           # Polling, charts, chat
├── requirements.txt
├── .env.example
└── README.md
```

---

## Setup

### 1. Prerequisites

- Python 3.9 or newer
- A **GitHub Personal Access Token** with `repo` and `read:org` scopes
  Generate one at: https://github.com/settings/tokens
- *(Optional)* OpenAI API key for richer AI chat / comment grading
  Get one at: https://platform.openai.com/api-keys
- *(Optional)* Ollama running locally if you'd prefer a self-hosted LLM

### 2. Install

```bash
cd pr-inequality-detector
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure

```bash
cp .env.example .env
```

Open `.env` and fill in:

```bash
GITHUB_TOKEN=ghp_yourTokenHere

# Pick one provider — or leave AI_PROVIDER unset and Claude will auto-detect
AI_PROVIDER=openai
OPENAI_API_KEY=sk-yourKeyHere

# Or use Ollama (no key needed)
# AI_PROVIDER=ollama
# OLLAMA_MODEL=llama3.2
# OLLAMA_HOST=http://localhost:11434

# Or skip AI entirely — rule-based fallback still works
# AI_PROVIDER=rule-based
```

### 4. Run

```bash
cd backend
python app.py
```

Open **http://localhost:5000** in your browser.

### 5. Analyze a repo

- Enter any public repo (e.g. `facebook/react`, `vercel/next.js`, your own org's repo)
- Pick how many recent PRs to scan (5–100)
- Click **Run Analysis**
- Watch the progress bar; the dashboard renders when complete
- Ask the AI assistant questions like *"Who is overloaded?"* or *"Which PRs are risky?"*

---

## How It Works

```
User → Flask /api/analyze (queues job)
        ↓
Background thread → GitHub REST API
        - List PRs (paginated)
        - Per PR: reviews + review comments + issue comments + files
        ↓
analytics.run_full_analytics()
        - inequality, delays, quality, risk, bias, recs, burnout
        - composite team health score
        ↓
SQLite (payload as JSON)
        ↓
Frontend polls /api/analyze/{id}/status every 1.3s
        ↓
On complete: /api/analyze/{id}/results → renderAll()
```

The AI chat endpoint condenses the analytics JSON to ~14k characters and instructs the LLM to ground every answer in the provided context — so it never hallucinates reviewer names or PR numbers.

---

## Demo Tips for Hackathon Judges

- **`facebook/react`** with 30 PRs is the default — gives dramatic, real numbers in ~30 seconds
- Try **`vercel/next.js`** to compare two large OSS repos
- A small private repo with 10–20 PRs shows the dashboard in <10 seconds — ideal for quick demos
- The team-health KPI tile is the headline number — show that first
- The AI chat is the closing flourish; the suggestion chips are pre-loaded with the most impressive queries

---

## Troubleshooting

**`Invalid GitHub token`** — Your token is missing scopes or has expired. Regenerate with `repo` and `read:org` checked.

**`Rate limit exceeded`** — GitHub allows 5,000 requests/hour for authenticated users. Lower the PR count or wait an hour. The client auto-retries with backoff.

**AI chat replies feel mechanical** — You're on the rule-based fallback. Add `OPENAI_API_KEY` to `.env` and restart for natural-language answers.

**`Address already in use`** — Another process is on port 5000. Run `PORT=5050 python app.py`.

**Analysis stuck on "fetching"** — Open Network tab; if GitHub is slow, jobs can take 60–90s for 100 PRs on a busy repo. Check the Flask console for errors.

---

## API Endpoints

| Method | Path                              | Purpose                          |
| ------ | --------------------------------- | -------------------------------- |
| GET    | `/api/health`                     | App + AI provider status         |
| POST   | `/api/analyze`                    | Queue an analysis job            |
| GET    | `/api/analyze/<id>/status`        | Poll job progress                |
| GET    | `/api/analyze/<id>/results`       | Full analytics JSON              |
| POST   | `/api/chat/<id>`                  | Ask the AI assistant             |
| GET    | `/api/chat/<id>/history`          | Chat thread for an analysis      |
| GET    | `/api/analyses`                   | Recent runs                      |

---

## License

Built for Mind AIthon 2026. Free to fork, study, and extend.

## Credits

- Chart.js · Fraunces (Google Fonts) · Inter Tight · JetBrains Mono
- GitHub REST API · OpenAI · Ollama
