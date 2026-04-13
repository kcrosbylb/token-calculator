"""
Drop-in wrapper around anthropic.Anthropic that automatically POSTs real
token usage to the calculator server after every API call.

Usage
-----
Replace your existing client calls with the `ask()` function below,
or wrap your own client using `log_usage()` directly.

The POST to /ingest is fire-and-forget with a short timeout — it will
NEVER raise or break your actual API call even if the server is down.
"""

import anthropic
import requests

CALC_URL = "http://localhost:5000"   # change if your server runs elsewhere

client = anthropic.Anthropic()       # uses ANTHROPIC_API_KEY from env


def log_usage(model: str, usage: anthropic.types.Usage) -> None:
    """Post real token counts to the calculator. Safe to call from anywhere."""
    try:
        requests.post(
            f"{CALC_URL}/ingest",
            json={
                "model":         model,
                "input_tokens":  usage.input_tokens,
                "output_tokens": usage.output_tokens,
            },
            timeout=2,
        )
    except Exception:
        pass  # logging must never break the caller


def ask(prompt: str, model: str = "claude-sonnet-4-6",
        max_tokens: int = 1024, **kwargs) -> anthropic.types.Message:
    """
    Thin wrapper over client.messages.create that logs usage automatically.
    All keyword args are forwarded to the API (system, temperature, etc.).
    """
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
        **kwargs,
    )
    log_usage(model, response.usage)
    return response


# ── Quick smoke test ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    resp = ask("What is 2 + 2? Answer in one word.")
    print("Reply  :", resp.content[0].text)
    print("Tokens :", resp.usage.input_tokens, "in /", resp.usage.output_tokens, "out")
    print("Logged to", CALC_URL)
