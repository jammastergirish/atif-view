"""The viewer is a local HTTP server over trajectories atif-make produces."""

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from atif_make import corpus

from atif_view.viewer import serve

LOG = """{"timestamp": "2026-05-01T12:00:00Z", "type": "session_meta", "payload": {"session_id": "c", "cli_version": "1", "cwd": "/w"}}
{"timestamp": "2026-05-01T12:00:01Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]}}
{"timestamp": "2026-05-01T12:00:02Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hi back"}]}}
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url: str):
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, response.read()


@pytest.fixture
def server(tmp_path):
    log = tmp_path / "session.jsonl"
    log.write_text(LOG)
    entries = corpus.scan([log])
    assert entries, "fixture log was not recognised"

    port = _free_port()
    thread = threading.Thread(
        target=serve, kwargs={"entries": entries, "port": port, "open_browser": False},
        daemon=True,
    )
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):  # wait for the socket to accept
        try:
            _get(base + "/api/index")
            break
        except (urllib.error.URLError, ConnectionError):
            time.sleep(0.05)
    else:
        pytest.fail("viewer did not start")
    return base


def test_serves_the_page(server):
    status, body = _get(server + "/")
    assert status == 200
    assert b"<title>atif-view</title>" in body


def test_index_lists_the_session(server):
    _, body = _get(server + "/api/index")
    rows = json.loads(body)
    assert len(rows) == 1
    assert rows[0]["agent"] == "codex"


def test_converts_on_demand(server):
    _, body = _get(server + "/api/trajectory?i=0")
    trajectory = json.loads(body)
    assert trajectory["schema_version"] == "ATIF-v1.7"
    assert [s["source"] for s in trajectory["steps"]] == ["user", "agent"]


def test_unknown_session_is_reported_not_crashed(server):
    _, body = _get(server + "/api/trajectory?i=999")
    assert json.loads(body)["error"]


def test_reveal_rejects_a_missing_path(server):
    with pytest.raises(urllib.error.HTTPError) as caught:
        _get(server + "/api/reveal?path=%2Fnope%2Fnot%2Fhere")
    assert caught.value.code == 404


def test_reveal_rejects_an_empty_path(server):
    with pytest.raises(urllib.error.HTTPError) as caught:
        _get(server + "/api/reveal?path=")
    assert caught.value.code == 400


def _lan_address() -> str | None:
    """This host's outbound interface address, or None if there is no network.

    Connecting a UDP socket sends nothing; it just selects a route so the local
    address can be read back.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 80))  # TEST-NET-1, never routed
            address = s.getsockname()[0]
        except OSError:
            return None
    return None if address.startswith("127.") else address


def test_binds_loopback_only(server):
    """Session logs carry source code and tool output; never expose them."""
    address = _lan_address()
    if address is None:
        pytest.skip("no non-loopback interface to test against")
    port = int(server.rsplit(":", 1)[1])
    with socket.socket() as s:
        s.settimeout(2)
        # Reaching the server over a routable address would mean it is exposed.
        assert s.connect_ex((address, port)) != 0
