"""
Tests for the Anthropic proxy endpoint and usage logging.

Run with:  python3 -m pytest test_proxy.py -v

No real API calls are made — requests.post is mocked throughout.
Each test gets its own isolated SQLite DB so nothing touches usage.db.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

import app as server


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    """Give each test a fresh, empty SQLite DB."""
    orig = server.DB_PATH
    server.DB_PATH = str(tmp_path / "test.db")
    server.init_db()
    yield
    server.DB_PATH = orig


@pytest.fixture(autouse=True)
def reset_session():
    """Reset in-memory session counters between tests."""
    yield
    server._session.update({
        "queries": 0, "input_tokens": 0,
        "output_tokens": 0, "total_cost": 0.0, "by_model": {},
    })


@pytest.fixture()
def client():
    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        yield c


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_upstream(input_tokens=100, output_tokens=50, status=200, model="claude-sonnet-4-6"):
    """Build a mock non-streaming upstream response."""
    payload = {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "mock reply"}],
        "model": model,
        "stop_reason": "end_turn",
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }
    m = MagicMock()
    m.status_code = status
    m.content = json.dumps(payload).encode()
    m.headers = {"content-type": "application/json"}
    m.json.return_value = payload
    return m


def make_streaming_upstream(input_tokens=200, output_tokens=75):
    """Build a mock streaming upstream response with realistic SSE events."""
    events = [
        f'data: {json.dumps({"type":"message_start","message":{"usage":{"input_tokens":input_tokens}}})}'.encode(),
        b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"mock"}}',
        b'data: {"type":"content_block_stop","index":0}',
        f'data: {json.dumps({"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":output_tokens}})}'.encode(),
        b'data: {"type":"message_stop"}',
    ]
    m = MagicMock()
    m.status_code = 200
    m.headers = {"content-type": "text/event-stream"}
    m.iter_lines.return_value = iter(events)
    return m


# ── Tests ─────────────────────────────────────────────────────────────────────

@patch("app.req_lib.post")
def test_proxy_logs_real_tokens_to_db(mock_post, client):
    """Proxy call writes input/output tokens with source='api' to the DB."""
    mock_post.return_value = make_upstream(input_tokens=4_000, output_tokens=800)

    resp = client.post(
        "/v1/messages",
        json={"model": "claude-sonnet-4-6", "max_tokens": 1024,
              "messages": [{"role": "user", "content": "Hello"}]},
        headers={"x-api-key": "test-key", "anthropic-version": "2023-06-01"},
    )

    assert resp.status_code == 200

    # /usage daily must show this call
    usage = client.get("/usage").get_json()
    assert usage["alltime"]["input_tokens"] == 4_000
    assert usage["alltime"]["output_tokens"] == 800
    assert len(usage["daily"]) == 1
    assert usage["daily"][0]["api_calls"] == 1


@patch("app.req_lib.post")
def test_proxy_forwards_request_to_anthropic(mock_post, client):
    """Proxy must call api.anthropic.com, not any other host."""
    mock_post.return_value = make_upstream()

    client.post(
        "/v1/messages",
        json={"model": "claude-sonnet-4-6", "max_tokens": 10,
              "messages": [{"role": "user", "content": "Hi"}]},
        headers={"x-api-key": "test-key"},
    )

    mock_post.assert_called_once()
    url = mock_post.call_args[0][0]
    assert "api.anthropic.com/v1/messages" in url


@patch("app.req_lib.post")
def test_proxy_forwards_api_key_header(mock_post, client):
    """x-api-key must be forwarded to Anthropic, not stripped."""
    mock_post.return_value = make_upstream()

    client.post(
        "/v1/messages",
        json={"model": "claude-sonnet-4-6", "messages": []},
        headers={"x-api-key": "sk-ant-secret"},
    )

    forwarded_headers = mock_post.call_args[1]["headers"]
    assert forwarded_headers.get("x-api-key") == "sk-ant-secret"


@patch("app.req_lib.post")
def test_manual_calculate_excluded_from_daily_log(mock_post, client):
    """
    /calculate entries are manual estimates (source='manual').
    They must NEVER appear in the real-usage daily log.
    """
    client.post("/calculate", json={
        "model": "claude-sonnet-4-6",
        "input_tokens": 99_999,
        "output_tokens": 99_999,
    })

    usage = client.get("/usage").get_json()
    assert usage["alltime"]["queries"] == 0, "manual estimate leaked into real-usage log"
    assert usage["daily"] == []


@patch("app.req_lib.post")
def test_proxy_does_not_log_failed_calls(mock_post, client):
    """4xx/5xx responses from Anthropic must be proxied without logging."""
    err = MagicMock()
    err.status_code = 401
    err.content = b'{"error":{"type":"authentication_error","message":"Invalid API key"}}'
    err.headers = {"content-type": "application/json"}
    mock_post.return_value = err

    resp = client.post(
        "/v1/messages",
        json={"model": "claude-sonnet-4-6", "messages": []},
        headers={"x-api-key": "bad-key"},
    )

    assert resp.status_code == 401
    assert server._session["queries"] == 0
    usage = client.get("/usage").get_json()
    assert usage["alltime"]["queries"] == 0


@patch("app.req_lib.post")
def test_streaming_proxy_logs_usage(mock_post, client):
    """Streaming calls must log input+output tokens after all chunks are forwarded."""
    mock_post.return_value = make_streaming_upstream(input_tokens=200, output_tokens=75)

    resp = client.post(
        "/v1/messages",
        json={"model": "claude-sonnet-4-6", "stream": True,
              "messages": [{"role": "user", "content": "stream this"}]},
        headers={"x-api-key": "test-key"},
    )

    # Consume the full response so the generator runs to completion
    _ = resp.data

    usage = client.get("/usage").get_json()
    assert usage["alltime"]["input_tokens"] == 200
    assert usage["alltime"]["output_tokens"] == 75
    assert usage["daily"][0]["api_calls"] == 1


@patch("app.req_lib.post")
def test_cost_calculation_is_deterministic(mock_post, client):
    """
    Cost = (tokens / 1_000_000) * price_per_mtok.
    For sonnet-4-6: $3.00 input / $15.00 output per MTok.
    """
    mock_post.return_value = make_upstream(input_tokens=1_000_000, output_tokens=1_000_000)

    client.post(
        "/v1/messages",
        json={"model": "claude-sonnet-4-6", "messages": []},
        headers={"x-api-key": "test-key"},
    )

    usage = client.get("/usage").get_json()
    assert usage["alltime"]["total_cost"] == pytest.approx(3.00 + 15.00)
