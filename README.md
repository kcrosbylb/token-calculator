# Anthropic Token Cost Calculator

A local web app that estimates Claude API token costs and automatically tracks real usage across every project on your machine — with zero code changes to your existing code.

## How it works

The app runs a Flask server on `localhost:5000` that does two things:

1. **Calculator UI** — enter input/output token counts to estimate cost, compare models side by side, and see projected spend.
2. **Transparent proxy** — the Anthropic SDK respects the `ANTHROPIC_BASE_URL` environment variable. Point it at `localhost:5000` and every `client.messages.create()` call in every project routes through the proxy, which logs real token usage to a local SQLite database before forwarding the request to `api.anthropic.com`.

---

## Prerequisites

- Python 3.10+
- `flask` and `requests` (both installed system-wide or in a venv)
- `gh` CLI authenticated (only needed for the initial GitHub push)

---

## Installation

```bash
# 1. Clone the repo
git clone git@github.com:kcrosbylb/token-calculator.git
cd token-calculator

# 2. Install dependencies
pip install -r requirements.txt

# 3. Add the proxy env var and shell aliases to your profile
#    (skip if already present — check with: grep ANTHROPIC_BASE_URL ~/.bashrc)
cat >> ~/.bashrc << 'EOF'

# ── Anthropic token tracker ───────────────────────────────────────────────────
export ANTHROPIC_BASE_URL=http://localhost:5000

alias tracker-start='cd ~/token-calculator && python3 app.py &'
alias tracker-stop='pkill -f "python3 app.py" && echo "tracker stopped"'
alias tracker-log='sqlite3 ~/token-calculator/usage.db "SELECT date(queried_at) day, model, sum(input_tokens) input, sum(output_tokens) output FROM queries WHERE source='"'"'api'"'"' GROUP BY 1,2 ORDER BY 1 DESC LIMIT 20;"'
EOF

source ~/.bashrc
```

---

## Starting the server

```bash
tracker-start
# → server running at http://localhost:5000
```

Or run it in the foreground (shows request logs):

```bash
python3 app.py
```

---

## Usage

### Calculator UI

Open **http://localhost:5000** in your browser.

- **Calculate** — enter a model, input tokens, and output tokens to get a cost breakdown.
- **Compare all models** — see all three models side by side for the same token counts.
- **Real API usage** card — daily history of actual token usage logged through the proxy.
- **Session usage** card — in-memory counters since the server last started.

### Automatic proxy tracking

With `ANTHROPIC_BASE_URL=http://localhost:5000` set, your existing code requires **no changes**:

```python
import anthropic

client = anthropic.Anthropic()  # picks up ANTHROPIC_BASE_URL automatically

response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello"}],
)
# → usage logged to usage.db automatically
```

Both streaming (`stream=True`) and non-streaming calls are supported.

> **Note:** the server must be running before making API calls. If `localhost:5000` is unreachable, the SDK will raise a connection error. Run `tracker-start` at the beginning of a working session.

### Quick terminal log

```bash
tracker-log
# day         | model               | input   | output
# 2026-04-13  | claude-sonnet-4-6   | 42000   | 8300
```

---

## Updating prices

Prices are defined at the top of `app.py`:

```python
PRICING = {
    "claude-opus-4-6":   (15.00, 75.00),   # (input $/MTok, output $/MTok)
    "claude-sonnet-4-6": ( 3.00, 15.00),
    "claude-haiku-4-5":  ( 0.80,  4.00),
}
```

Verify current prices at [anthropic.com/pricing](https://www.anthropic.com/pricing) and update this dict. New models added here appear automatically in the UI dropdown.

---

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Calculator UI |
| `POST` | `/v1/messages` | Anthropic proxy — forwards to `api.anthropic.com`, logs usage |
| `POST` | `/calculate` | Manual cost estimate (logged as `source='manual'`, excluded from daily real-usage log) |
| `POST` | `/compare` | Cost comparison across all models for given token counts |
| `GET` | `/usage` | Session counters, all-time DB totals, and 30-day daily breakdown |
| `POST` | `/usage/reset` | Reset in-memory session counters (DB history is never deleted) |

---

## Database

Usage is stored in `usage.db` (SQLite, WAL mode) in the project root. It is gitignored — it lives only on your machine.

```bash
# Inspect raw data
sqlite3 usage.db "SELECT * FROM queries ORDER BY queried_at DESC LIMIT 10;"
```

Schema:

```sql
CREATE TABLE queries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    model         TEXT    NOT NULL,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    input_cost    REAL    NOT NULL,
    output_cost   REAL    NOT NULL,
    total_cost    REAL    NOT NULL,
    source        TEXT    NOT NULL DEFAULT 'manual',  -- 'api' | 'manual'
    queried_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);
```

`source='api'` rows come from the proxy (real usage). `source='manual'` rows come from the calculator UI (estimates). The daily history in the UI shows only `source='api'` rows.

---

## Running tests

```bash
python3 -m pytest test_proxy.py -v
```

Tests mock all outbound HTTP — no real API calls are made and `usage.db` is not touched.

---

## Shell aliases reference

| Alias | Action |
|-------|--------|
| `tracker-start` | Start server in the background |
| `tracker-stop` | Kill the server |
| `tracker-log` | Print last 20 days of real usage from the DB |
