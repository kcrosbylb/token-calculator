from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

# USD per million tokens — verify at https://www.anthropic.com/pricing
PRICING = {
    "claude-opus-4-6":   (15.00, 75.00),
    "claude-sonnet-4-6": ( 3.00, 15.00),
    "claude-haiku-4-5":  ( 0.80,  4.00),
}


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

    return jsonify({
        "model":              model,
        "input_tokens":       input_tokens,
        "output_tokens":      output_tokens,
        "input_cost":         input_cost,
        "output_cost":        output_cost,
        "total_cost":         input_cost + output_cost,
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
            "model":        model,
            "input_cost":   input_cost,
            "output_cost":  output_cost,
            "total_cost":   input_cost + output_cost,
        })

    return jsonify(results)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
