import os
import sqlite3
from flask import Flask, render_template, request, jsonify, g

app = Flask(__name__)

# USD per million tokens — verify at https://www.anthropic.com/pricing
PRICING = {
    "claude-opus-4-6":   (15.00, 75.00),
    "claude-sonnet-4-6": ( 3.00, 15.00),
    "claude-haiku-4-5":  ( 0.80,  4.00),
}

DB_PATH = os.path.join(os.path.dirname(__file__), "usage.db")

# In-memory session counters — resets on server restart, tracks ALL calls
_session = {
    "queries":       0,
    "input_tokens":  0,
    "output_tokens": 0,
    "total_cost":    0.0,
    "by_model": {m: {"queries": 0, "input_tokens": 0, "output_tokens": 0, "total_cost": 0.0}
                 for m in PRICING},
}


# ── SQLite ────────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        # WAL mode: writes survive crashes; readers never block writers
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS queries (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                model         TEXT    NOT NULL,
                input_tokens  INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                input_cost    REAL    NOT NULL,
                output_cost   REAL    NOT NULL,
                total_cost    REAL    NOT NULL,
                -- 'api'    = real usage ingested from response.usage (counted in daily log)
                -- 'manual' = manual estimate entered via the calculator UI
                source        TEXT    NOT NULL DEFAULT 'manual',
                queried_at    TEXT    NOT NULL
                                DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_queried_at ON queries (queried_at)"
        )
        conn.commit()


init_db()


# ── Recording ─────────────────────────────────────────────────────────────────

def _record(model: str, input_tokens: int, output_tokens: int,
            input_cost: float, output_cost: float, total: float,
            source: str = "manual") -> None:
    # Session counters (all sources)
    _session["queries"]       += 1
    _session["input_tokens"]  += input_tokens
    _session["output_tokens"] += output_tokens
    _session["total_cost"]    += total
    m = _session["by_model"][model]
    m["queries"]       += 1
    m["input_tokens"]  += input_tokens
    m["output_tokens"] += output_tokens
    m["total_cost"]    += total

    # Durable write — WAL ensures this survives a crash
    db = get_db()
    db.execute(
        "INSERT INTO queries "
        "(model, input_tokens, output_tokens, input_cost, output_cost, total_cost, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (model, input_tokens, output_tokens, input_cost, output_cost, total, source),
    )
    db.commit()


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", models=list(PRICING.keys()))


@app.route("/calculate", methods=["POST"])
def calculate():
    """Manual estimate — logged as source='manual', not counted in daily API log."""
    data = request.get_json()
    model = data.get("model", "")
    try:
        input_tokens  = int(data.get("input_tokens",  0))
        output_tokens = int(data.get("output_tokens", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Token counts must be integers."}), 400

    if model not in PRICING:
        return jsonify({"error": f"Unknown model: {model}"}), 400

    in_price, out_price = PRICING[model]
    input_cost  = (input_tokens  / 1_000_000) * in_price
    output_cost = (output_tokens / 1_000_000) * out_price
    total       = input_cost + output_cost

    _record(model, input_tokens, output_tokens, input_cost, output_cost, total, source="manual")

    return jsonify({
        "model":              model,
        "input_tokens":       input_tokens,
        "output_tokens":      output_tokens,
        "input_cost":         input_cost,
        "output_cost":        output_cost,
        "total_cost":         total,
        "in_price_per_mtok":  in_price,
        "out_price_per_mtok": out_price,
    })


@app.route("/ingest", methods=["POST"])
def ingest():
    """
    Real API usage endpoint. Call this with the exact values from response.usage
    after every Anthropic API call. These are the only rows counted in the
    daily history — guaranteed to reflect actual token consumption.

    POST body: { "model": "claude-sonnet-4-6", "input_tokens": N, "output_tokens": N }
    """
    data = request.get_json()
    model = data.get("model", "")
    try:
        input_tokens  = int(data.get("input_tokens",  0))
        output_tokens = int(data.get("output_tokens", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Token counts must be integers."}), 400

    if model not in PRICING:
        return jsonify({"error": f"Unknown model '{model}'. Known: {list(PRICING)}"}), 400

    in_price, out_price = PRICING[model]
    input_cost  = (input_tokens  / 1_000_000) * in_price
    output_cost = (output_tokens / 1_000_000) * out_price
    total       = input_cost + output_cost

    _record(model, input_tokens, output_tokens, input_cost, output_cost, total, source="api")

    return jsonify({
        "logged":        True,
        "model":         model,
        "input_tokens":  input_tokens,
        "output_tokens": output_tokens,
        "total_cost":    total,
    })


@app.route("/compare", methods=["POST"])
def compare():
    data = request.get_json()
    try:
        input_tokens  = int(data.get("input_tokens",  0))
        output_tokens = int(data.get("output_tokens", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Token counts must be integers."}), 400

    results = []
    for model, (in_price, out_price) in PRICING.items():
        input_cost  = (input_tokens  / 1_000_000) * in_price
        output_cost = (output_tokens / 1_000_000) * out_price
        results.append({
            "model":       model,
            "input_cost":  input_cost,
            "output_cost": output_cost,
            "total_cost":  input_cost + output_cost,
        })

    return jsonify(results)


@app.route("/usage", methods=["GET"])
def usage():
    db = get_db()

    # All-time totals (API only — real usage)
    row = db.execute(
        "SELECT count(*) q, sum(input_tokens) i, sum(output_tokens) o, sum(total_cost) c "
        "FROM queries WHERE source='api'"
    ).fetchone()
    alltime = {
        "queries":       row["q"] or 0,
        "input_tokens":  row["i"] or 0,
        "output_tokens": row["o"] or 0,
        "total_cost":    row["c"] or 0.0,
    }

    # Daily breakdown — last 30 days, API calls only
    daily_rows = db.execute(
        "SELECT date(queried_at) day, "
        "  count(*) api_calls, "
        "  sum(input_tokens) i, "
        "  sum(output_tokens) o, "
        "  sum(total_cost) c "
        "FROM queries "
        "WHERE source='api' "
        "GROUP BY date(queried_at) "
        "ORDER BY day DESC "
        "LIMIT 30"
    ).fetchall()
    daily = [
        {
            "date":          r["day"],
            "api_calls":     r["api_calls"],
            "input_tokens":  r["i"],
            "output_tokens": r["o"],
            "total_cost":    r["c"],
        }
        for r in daily_rows
    ]

    return jsonify({
        "session": _session,
        "alltime": alltime,
        "daily":   daily,
    })


@app.route("/usage/reset", methods=["POST"])
def usage_reset():
    """Resets in-memory session counters only. DB history is never deleted."""
    _session["queries"]       = 0
    _session["input_tokens"]  = 0
    _session["output_tokens"] = 0
    _session["total_cost"]    = 0.0
    for m in _session["by_model"]:
        _session["by_model"][m] = {"queries": 0, "input_tokens": 0, "output_tokens": 0, "total_cost": 0.0}
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
