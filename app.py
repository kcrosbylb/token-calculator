import json as json_mod
import os
import sqlite3

import requests as req_lib
from flask import Flask, Response, render_template, request, jsonify, g, stream_with_context

app = Flask(__name__)

# USD per million tokens — verify at https://www.anthropic.com/pricing
PRICING = {
    "claude-opus-4-6":   (15.00, 75.00),
    "claude-sonnet-4-6": ( 3.00, 15.00),
    "claude-haiku-4-5":  ( 0.80,  4.00),
}

ANTHROPIC_API = "https://api.anthropic.com"

DB_PATH = os.path.join(os.path.dirname(__file__), "usage.db")

# In-memory session counters — resets on server restart
_session = {
    "queries":       0,
    "input_tokens":  0,
    "output_tokens": 0,
    "total_cost":    0.0,
    "by_model":      {},   # populated dynamically as models are seen
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
                source        TEXT    NOT NULL DEFAULT 'manual',
                queried_at    TEXT    NOT NULL
                                DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_queried_at ON queries (queried_at)")
        conn.commit()


init_db()


# ── Recording ─────────────────────────────────────────────────────────────────

def _costs(model: str, input_tokens: int, output_tokens: int):
    """Return (input_cost, output_cost, total). Zero if model not in PRICING."""
    if model in PRICING:
        ip, op = PRICING[model]
        ic = (input_tokens  / 1_000_000) * ip
        oc = (output_tokens / 1_000_000) * op
    else:
        ic = oc = 0.0
    return ic, oc, ic + oc


def _record(model: str, input_tokens: int, output_tokens: int,
            input_cost: float, output_cost: float, total: float,
            source: str = "manual") -> None:
    # Session
    _session["queries"]       += 1
    _session["input_tokens"]  += input_tokens
    _session["output_tokens"] += output_tokens
    _session["total_cost"]    += total
    if model not in _session["by_model"]:
        _session["by_model"][model] = {"queries": 0, "input_tokens": 0,
                                       "output_tokens": 0, "total_cost": 0.0}
    m = _session["by_model"][model]
    m["queries"]       += 1
    m["input_tokens"]  += input_tokens
    m["output_tokens"] += output_tokens
    m["total_cost"]    += total

    # Persistent DB (WAL — survives crashes)
    db = get_db()
    db.execute(
        "INSERT INTO queries "
        "(model, input_tokens, output_tokens, input_cost, output_cost, total_cost, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (model, input_tokens, output_tokens, input_cost, output_cost, total, source),
    )
    db.commit()


# ── Anthropic proxy ───────────────────────────────────────────────────────────

def _proxy_headers() -> dict:
    """Forward only the headers the Anthropic API cares about."""
    keep = ("x-api-key", "anthropic-version", "anthropic-beta", "content-type")
    hdrs = {k: request.headers[k] for k in keep if k in request.headers}
    hdrs.setdefault("content-type", "application/json")
    return hdrs


@app.route("/v1/messages", methods=["POST"])
def proxy_messages():
    """
    Transparent proxy for the Anthropic messages API.
    The SDK hits this automatically when ANTHROPIC_BASE_URL=http://localhost:5000.
    Logs real token usage (source='api') after every successful call.
    """
    body    = request.get_json(force=True, silent=True) or {}
    model   = body.get("model", "unknown")
    is_stream = body.get("stream", False)

    upstream = req_lib.post(
        f"{ANTHROPIC_API}/v1/messages",
        headers=_proxy_headers(),
        json=body,
        stream=is_stream,
        timeout=120,
    )

    if not is_stream:
        # ── Non-streaming ──────────────────────────────────────────────────────
        if upstream.status_code == 200:
            data = upstream.json()
            usage = data.get("usage", {})
            ic, oc, total = _costs(model, usage.get("input_tokens", 0),
                                          usage.get("output_tokens", 0))
            _record(model, usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                    ic, oc, total, source="api")

        return Response(
            upstream.content,
            status=upstream.status_code,
            content_type=upstream.headers.get("content-type", "application/json"),
        )

    # ── Streaming ─────────────────────────────────────────────────────────────
    # Pass each SSE chunk straight to the client while capturing usage events.
    # Logging happens after the final chunk is forwarded — no buffering delay.
    def generate():
        input_tokens  = 0
        output_tokens = 0

        for raw in upstream.iter_lines():
            if not raw:
                continue
            yield raw + b"\n\n"

            if raw.startswith(b"data: "):
                try:
                    ev = json_mod.loads(raw[6:])
                    t  = ev.get("type")
                    if t == "message_start":
                        input_tokens = ev["message"]["usage"]["input_tokens"]
                    elif t == "message_delta":
                        output_tokens = ev.get("usage", {}).get("output_tokens", 0)
                except Exception:
                    pass

        # All chunks forwarded — now write to DB inside the still-live app context
        ic, oc, total = _costs(model, input_tokens, output_tokens)
        _record(model, input_tokens, output_tokens, ic, oc, total, source="api")

    return Response(
        stream_with_context(generate()),
        status=upstream.status_code,
        content_type="text/event-stream",
    )


# ── Calculator routes ─────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", models=list(PRICING.keys()))


@app.route("/calculate", methods=["POST"])
def calculate():
    """Manual estimate — source='manual', not counted in real-usage daily log."""
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

    daily_rows = db.execute(
        "SELECT date(queried_at) day, count(*) api_calls, "
        "sum(input_tokens) i, sum(output_tokens) o, sum(total_cost) c "
        "FROM queries WHERE source='api' "
        "GROUP BY date(queried_at) ORDER BY day DESC LIMIT 30"
    ).fetchall()
    daily = [
        {"date": r["day"], "api_calls": r["api_calls"],
         "input_tokens": r["i"], "output_tokens": r["o"], "total_cost": r["c"]}
        for r in daily_rows
    ]

    return jsonify({"session": _session, "alltime": alltime, "daily": daily})


@app.route("/usage/reset", methods=["POST"])
def usage_reset():
    _session["queries"]       = 0
    _session["input_tokens"]  = 0
    _session["output_tokens"] = 0
    _session["total_cost"]    = 0.0
    _session["by_model"]      = {}
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
