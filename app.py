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

# In-memory session counters — resets on server restart
_session = {
    "queries":       0,
    "input_tokens":  0,
    "output_tokens": 0,
    "total_cost":    0.0,
    "by_model": {m: {"queries": 0, "input_tokens": 0, "output_tokens": 0, "total_cost": 0.0}
                 for m in PRICING},
}


# ── SQLite helpers ────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    """Return a per-request SQLite connection (stored on Flask's g)."""
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS queries (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                model         TEXT    NOT NULL,
                input_tokens  INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                input_cost    REAL    NOT NULL,
                output_cost   REAL    NOT NULL,
                total_cost    REAL    NOT NULL,
                queried_at    TEXT    NOT NULL
                                DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
            )
        """)
        conn.commit()


init_db()


# ── Recording ─────────────────────────────────────────────────────────────────

def _record(model: str, input_tokens: int, output_tokens: int,
            input_cost: float, output_cost: float, total: float) -> None:
    # In-memory session
    _session["queries"]       += 1
    _session["input_tokens"]  += input_tokens
    _session["output_tokens"] += output_tokens
    _session["total_cost"]    += total
    m = _session["by_model"][model]
    m["queries"]       += 1
    m["input_tokens"]  += input_tokens
    m["output_tokens"] += output_tokens
    m["total_cost"]    += total

    # Persistent DB
    db = get_db()
    db.execute(
        "INSERT INTO queries (model, input_tokens, output_tokens, input_cost, output_cost, total_cost) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (model, input_tokens, output_tokens, input_cost, output_cost, total),
    )
    db.commit()


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", models=list(PRICING.keys()))


@app.route("/calculate", methods=["POST"])
def calculate():
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

    _record(model, input_tokens, output_tokens, input_cost, output_cost, total)

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

    # All-time totals from DB
    row = db.execute(
        "SELECT count(*) q, sum(input_tokens) i, sum(output_tokens) o, sum(total_cost) c FROM queries"
    ).fetchone()
    alltime = {
        "queries":       row["q"] or 0,
        "input_tokens":  row["i"] or 0,
        "output_tokens": row["o"] or 0,
        "total_cost":    row["c"] or 0.0,
    }

    # All-time by model
    by_model_rows = db.execute(
        "SELECT model, count(*) q, sum(input_tokens) i, sum(output_tokens) o, sum(total_cost) c "
        "FROM queries GROUP BY model"
    ).fetchall()
    alltime_by_model = {
        r["model"]: {
            "queries":       r["q"],
            "input_tokens":  r["i"],
            "output_tokens": r["o"],
            "total_cost":    r["c"],
        }
        for r in by_model_rows
    }

    # Daily breakdown — last 30 days
    daily_rows = db.execute(
        "SELECT date(queried_at) day, count(*) q, "
        "sum(input_tokens) i, sum(output_tokens) o, sum(total_cost) c "
        "FROM queries "
        "GROUP BY date(queried_at) "
        "ORDER BY day DESC "
        "LIMIT 30"
    ).fetchall()
    daily = [
        {
            "date":          r["day"],
            "queries":       r["q"],
            "input_tokens":  r["i"],
            "output_tokens": r["o"],
            "total_cost":    r["c"],
        }
        for r in daily_rows
    ]

    return jsonify({
        "session":          _session,
        "alltime":          alltime,
        "alltime_by_model": alltime_by_model,
        "daily":            daily,
    })


@app.route("/usage/reset", methods=["POST"])
def usage_reset():
    """Resets the in-memory session counters only. DB history is preserved."""
    _session["queries"]       = 0
    _session["input_tokens"]  = 0
    _session["output_tokens"] = 0
    _session["total_cost"]    = 0.0
    for m in _session["by_model"]:
        _session["by_model"][m] = {"queries": 0, "input_tokens": 0, "output_tokens": 0, "total_cost": 0.0}
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
