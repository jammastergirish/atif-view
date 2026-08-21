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


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    """Never let a test touch the real library, index or opened-file store."""
    from atif_make import corpus
    from atif_view import library, viewer

    monkeypatch.setattr(library, "LIBRARY_PATH", tmp_path / "library.json")
    monkeypatch.setattr(corpus, "OPENED_ROOT", tmp_path / "opened")
    monkeypatch.setattr(corpus, "INDEX_PATH", tmp_path / "index.json")


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
    assert b"<title>ATIF-View</title>" in body


def test_index_lists_the_session(server):
    rows = _sessions(server)
    assert len(rows) == 1
    assert rows[0]["agent"] == "codex"
    # Annotations arrive folded in, so the client never has to join.
    assert rows[0]["title"] == "" and rows[0]["tags"] == []


def _index(server: str) -> dict:
    return json.loads(_get(server + "/api/index")[1])


def _sessions(server: str) -> list:
    return _index(server)["sessions"]


def _first_key(server: str) -> str:
    return _sessions(server)[0]["key"]


def test_converts_on_demand(server):
    _, body = _get(server + f"/api/trajectory?id={_first_key(server)}")
    trajectory = json.loads(body)
    assert trajectory["schema_version"] == "ATIF-v1.7"
    assert [s["source"] for s in trajectory["steps"]] == ["user", "agent"]


def test_unknown_session_is_reported_not_crashed(server):
    _, body = _get(server + "/api/trajectory?id=nosuchkey")
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


def test_falls_back_when_the_default_port_is_busy(tmp_path):
    """Viewing a second session while a first is open is normal, not an error."""
    log = tmp_path / "s.jsonl"
    log.write_text(LOG)
    entries = corpus.scan([log])

    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen(1)
    busy = taken.getsockname()[1]
    try:
        thread = threading.Thread(
            target=serve,
            kwargs={"entries": entries, "port": busy, "open_browser": False,
                    "explicit_port": False},
            daemon=True,
        )
        thread.start()
        # It must come up on some nearby port rather than crashing. Poll the
        # whole range until a deadline: one attempt per port races a slow start.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            for candidate in range(busy + 1, busy + 20):
                try:
                    status, _ = _get(f"http://127.0.0.1:{candidate}/api/index")
                except (urllib.error.URLError, ConnectionError, OSError):
                    continue
                assert status == 200
                return
            time.sleep(0.1)
        pytest.fail("viewer did not fall back to a free port")
    finally:
        taken.close()


def test_explicit_port_in_use_is_reported_not_traced(tmp_path):
    """An explicitly requested port is honoured or reported, never moved."""
    log = tmp_path / "s.jsonl"
    log.write_text(LOG)
    entries = corpus.scan([log])

    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen(1)
    busy = taken.getsockname()[1]
    try:
        with pytest.raises(SystemExit) as caught:
            serve(entries, port=busy, open_browser=False, explicit_port=True)
        assert "already in use" in str(caught.value)
        assert "--port" in str(caught.value)
    finally:
        taken.close()


def test_raw_endpoint_returns_the_source(server):
    _, body = _get(server + f"/api/raw?id={_first_key(server)}")
    raw = json.loads(body)
    assert raw["path"].endswith("session.jsonl")
    assert not raw["truncated"]
    # The head of the actual log, not the converted trajectory.
    assert '"session_meta"' in raw["text"]


def test_raw_endpoint_truncates_a_large_file(tmp_path):
    """A rollout can be hundreds of MB; the panel must not ship the lot."""
    from atif_view.viewer import RAW_LIMIT, _Handler

    big = tmp_path / "big.jsonl"
    big.write_text(LOG + ("x" * (RAW_LIMIT + 1000)))
    assert big.stat().st_size > RAW_LIMIT


def test_files_endpoint_lists_subagents(tmp_path, server):
    _, body = _get(server + f"/api/files?id={_first_key(server)}")
    files = json.loads(body)
    roles = {f["role"] for f in files}
    assert "source" in roles
    assert all(f["size"] >= 0 for f in files)


def test_files_endpoint_finds_sibling_subagents(tmp_path):
    from atif_view.viewer import _associated_files

    source = tmp_path / "sess.jsonl"
    source.write_text(LOG)
    subagents = tmp_path / "sess" / "subagents"
    subagents.mkdir(parents=True)
    (subagents / "agent-abc.jsonl").write_text(LOG)
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "a.png").write_bytes(b"\x89PNG")

    found = _associated_files(source)
    roles = {f["name"]: f["role"] for f in found}
    assert roles["sess.jsonl"] == "source"
    assert roles["agent-abc.jsonl"] == "subagent"
    assert roles["a.png"] == "image"


def test_bad_index_on_new_endpoints(server):
    for path in ("/api/raw?id=nope", "/api/files?id=nope"):
        with pytest.raises(urllib.error.HTTPError) as caught:
            _get(server + path)
        assert caught.value.code == 404


def _post_json(url: str, body: dict):
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, json.loads(response.read())


def _post(url: str, data: bytes, filename: str):
    request = urllib.request.Request(
        url, data=data, method="POST", headers={"X-Filename": filename}
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, json.loads(response.read())


def test_open_adds_an_uploaded_log_to_the_index(server):
    """The Open button must add exactly what the CLI would."""
    before = len(_sessions(server))
    other = LOG.replace('"session_id": "c"', '"session_id": "dropped"')
    status, result = _post(server + "/api/open", other.encode(), "dropped.jsonl")
    assert status == 200
    assert result["added"] == 1
    after = _sessions(server)
    assert len(after) == before + 1
    # And it is immediately viewable, not merely listed.
    _, body = _get(server + f"/api/trajectory?id={result['keys'][0]}")
    assert json.loads(body)["schema_version"] == "ATIF-v1.7"


def test_open_rejects_a_file_that_is_not_a_log(server):
    with pytest.raises(urllib.error.HTTPError) as caught:
        _post(server + "/api/open", b'{"grade":1}\n', "grades.jsonl")
    assert caught.value.code == 415


def test_open_rejects_an_empty_upload(server):
    with pytest.raises(urllib.error.HTTPError) as caught:
        _post(server + "/api/open", b"", "nothing.jsonl")
    assert caught.value.code == 400


def test_open_neutralises_a_traversing_filename(server, tmp_path):
    """A client-supplied name must not decide where bytes land."""
    status, result = _post(
        server + "/api/open", LOG.encode(), "../../../../tmp/escaped.jsonl"
    )
    assert status == 200
    rows = {r["key"]: r for r in _sessions(server)}
    landed = Path(rows[result["keys"][0]]["path"])
    assert landed.name == "escaped.jsonl"
    assert "/tmp/escaped.jsonl" != str(landed)


def test_safe_name_reduces_to_a_leaf():
    from atif_view.viewer import _safe_name

    assert _safe_name("../../etc/passwd") == "passwd"
    assert _safe_name("/absolute/path.jsonl") == "path.jsonl"
    assert _safe_name("") == "upload"
    assert _safe_name("%2e%2e%2fetc%2fshadow") == "shadow"


def test_open_is_the_only_post_route(server):
    with pytest.raises(urllib.error.HTTPError) as caught:
        _post(server + "/api/anything", b"x", "x.jsonl")
    assert caught.value.code == 404


def test_reordering_the_index_does_not_change_what_a_link_opens(server):
    """The whole point of content keys: a link survives the list moving."""
    rows = _sessions(server)
    key = rows[0]["key"]
    _, before = _get(server + f"/api/trajectory?id={key}")

    from atif_view.viewer import _Handler

    _Handler.entries = list(reversed(_Handler.entries)) + list(_Handler.entries)
    _, after = _get(server + f"/api/trajectory?id={key}")
    assert json.loads(before)["session_id"] == json.loads(after)["session_id"]


def test_opening_the_same_file_twice_updates_rather_than_duplicates(server):
    """Content keys mean a file re-opened from a new place is still one entry."""
    payload = LOG.replace('"session_id": "c"', '"session_id": "twice"').encode()
    _post(server + "/api/open", payload, "first-name.jsonl")
    before = len(_sessions(server))
    _post(server + "/api/open", payload, "renamed.jsonl")
    assert len(_sessions(server)) == before


def test_opened_files_are_marked_and_stored_outside_temp(server):
    payload = LOG.replace('"session_id": "c"', '"session_id": "kept"').encode()
    _, result = _post(server + "/api/open", payload, "kept.jsonl")
    row = {r["key"]: r for r in _sessions(server)}[result["keys"][0]]
    assert row["origin"] == "opened"
    assert Path(row["path"]).is_file()
    # Under the opened store, keyed by content — not a temp directory.
    assert result["keys"][0] in row["path"]


def test_annotating_a_session_persists_and_comes_back_in_the_index(server):
    key = _first_key(server)
    status, record = _post_json(server + "/api/library", {
        "key": key, "title": "SOC2 web app", "folder": "Redwood/SOC2",
        "tags": ["security", "needs-review"], "starred": True,
    })
    assert status == 200
    assert record["title"] == "SOC2 web app"

    index = _index(server)
    row = {r["key"]: r for r in index["sessions"]}[key]
    assert row["title"] == "SOC2 web app"
    assert row["tags"] == ["security", "needs-review"]
    # The rail and filter bar are served alongside, already aggregated.
    assert index["folders"] == ["Redwood", "Redwood/SOC2"]
    assert {t["name"]: t["count"] for t in index["tags"]} == {
        "security": 1, "needs-review": 1,
    }


def test_annotating_without_a_key_is_rejected(server):
    with pytest.raises(urllib.error.HTTPError) as caught:
        _post_json(server + "/api/library", {"title": "orphan"})
    assert caught.value.code == 400


def test_deleting_annotations_keeps_a_scanned_session(server):
    key = _first_key(server)
    _post_json(server + "/api/library", {"key": key, "title": "Named"})
    request = urllib.request.Request(
        server + f"/api/library?id={key}", method="DELETE"
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 200
    rows = {r["key"]: r for r in _sessions(server)}
    # The transcript is still there — only what we said about it is gone.
    assert key in rows and rows[key]["title"] == ""


def test_deleting_an_opened_session_removes_its_copy(server):
    payload = LOG.replace('"session_id": "c"', '"session_id": "removable"').encode()
    _, result = _post(server + "/api/open", payload, "removable.jsonl")
    key = result["keys"][0]
    stored = Path({r["key"]: r for r in _sessions(server)}[key]["path"])
    assert stored.is_file()

    request = urllib.request.Request(
        server + f"/api/library?id={key}", method="DELETE"
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        assert json.loads(response.read())["removed_copy"] is True

    assert not stored.exists()
    assert key not in {r["key"] for r in _sessions(server)}


def test_an_opened_archive_is_unpacked_rather_than_stored_whole(server, tmp_path):
    """Storing the container would mean re-extracting to a temp directory on
    every start, and a directory scan does not look inside archives."""
    import zipfile

    bundle = tmp_path / "bundle.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr(
            "inner.jsonl", LOG.replace('"session_id": "c"', '"session_id": "inner"')
        )
        archive.writestr("manifest.json", '{"note": "travels with it"}')

    _, result = _post(server + "/api/open", bundle.read_bytes(), "bundle.zip")
    assert result["added"] == 1

    from atif_make import corpus

    stored = sorted(p.name for p in corpus.OPENED_ROOT.rglob("*") if p.is_file())
    assert "inner.jsonl" in stored
    assert "manifest.json" in stored          # siblings travel with it
    assert not any(n.endswith(".zip") for n in stored)


def test_an_opened_file_survives_a_rescan_of_the_default_roots(server, tmp_path):
    """A rescan replaces the index; anything brought in by hand must persist."""
    from atif_make import corpus

    payload = LOG.replace('"session_id": "c"', '"session_id": "persist"').encode()
    _post(server + "/api/open", payload, "persist.jsonl")

    kept = [e for e in corpus.load() if e.origin == "opened"]
    assert kept, "the opened file was not written to the index"
    # Its location alone is enough to classify it on a fresh look.
    assert corpus.describe(Path(kept[0].path)).origin == "opened"


def test_starring_steps_persists_on_the_session(server):
    key = _first_key(server)
    _post_json(server + "/api/library", {"key": key, "starred_steps": ["2", "5"]})
    row = {r["key"]: r for r in _sessions(server)}[key]
    assert row["starred_steps"] == ["2", "5"]


def test_a_starred_step_alone_is_worth_keeping(server):
    """A transcript with a starred step but no name must not be forgotten."""
    key = _first_key(server)
    _post_json(server + "/api/library", {"key": key, "starred_steps": ["7"]})
    row = {r["key"]: r for r in _sessions(server)}[key]
    assert row["title"] == "" and row["starred_steps"] == ["7"]


def test_renaming_from_the_transcript_uses_the_same_endpoint(server):
    """The heading and the table row must not disagree about a title."""
    key = _first_key(server)
    _post_json(server + "/api/library", {"key": key, "title": "Renamed here"})
    assert {r["key"]: r for r in _sessions(server)}[key]["title"] == "Renamed here"
