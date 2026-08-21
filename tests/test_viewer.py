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

# Built with json.dumps rather than written out: a hand-escaped argument string
# inside a triple-quoted literal loses a level of escaping and stops being JSON.
LOG += '{"timestamp": "2026-05-01T12:00:03Z", "type": "response_item", "payload": {"type": "function_call", "call_id": "call_a", "name": "shell", "arguments": "{\\"command\\": \\"ls\\"}"}}' + "\n" + '{"timestamp": "2026-05-01T12:00:04Z", "type": "response_item", "payload": {"type": "function_call_output", "call_id": "call_a", "output": "README.md"}}' + "\n"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url: str):
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, response.read()


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    """Never let a test touch the real library, index, settings or file store."""
    from atif_make import corpus
    from atif_view import config, library

    monkeypatch.setattr(library, "LIBRARY_PATH", tmp_path / "library.json")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.json")
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
    assert [s["source"] for s in trajectory["steps"]] == ["user", "agent", "agent"]


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


def test_deleting_one_file_from_an_archive_spares_its_siblings(server, tmp_path):
    """An unpacked archive shares a directory; removing one entry must not
    delete the other's file and leave its row pointing at nothing."""
    import zipfile

    bundle = tmp_path / "pair.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("one.jsonl", LOG.replace('"session_id": "c"', '"session_id": "one"'))
        archive.writestr("two.jsonl", LOG.replace('"session_id": "c"', '"session_id": "two"'))

    _, result = _post(server + "/api/open", bundle.read_bytes(), "pair.zip")
    assert result["added"] == 2
    first, second = result["keys"]

    rows = {r["key"]: Path(r["path"]) for r in _sessions(server)}
    request = urllib.request.Request(server + f"/api/library?id={first}", method="DELETE")
    with urllib.request.urlopen(request, timeout=5) as response:
        assert json.loads(response.read())["removed_copy"] is True

    assert not rows[first].exists()
    assert rows[second].exists(), "the sibling's file was deleted with it"
    # And it is still openable, not an orphaned row.
    assert _get(server + f"/api/raw?id={second}")[0] == 200


def test_deleting_the_last_file_clears_the_whole_store_directory(server, tmp_path):
    payload = LOG.replace('"session_id": "c"', '"session_id": "solo"').encode()
    _, result = _post(server + "/api/open", payload, "solo.jsonl")
    key = result["keys"][0]
    stored = {r["key"]: Path(r["path"]) for r in _sessions(server)}[key]

    request = urllib.request.Request(server + f"/api/library?id={key}", method="DELETE")
    urllib.request.urlopen(request, timeout=5).read()
    assert not stored.exists()
    assert not stored.parent.exists(), "the empty store directory was left behind"


# ---- settings and the AI gate -------------------------------------------------


def _try_post(url: str, body: dict) -> tuple[int, dict]:
    """POST that reports a refusal instead of raising, so it can be asserted on."""
    try:
        return _post_json(url, body)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _raise_unavailable():
    from atif_view import ai

    raise ai.Unavailable("none here")


@pytest.fixture
def credentialed(monkeypatch):
    """Pretend a credential is configured, without one being present."""
    from atif_view import ai

    monkeypatch.setattr(ai, "_client", lambda: object())
    return ai


def test_a_saved_key_with_no_sdk_says_which_piece_is_missing(server, monkeypatch):
    """The failure that read as "the save didn't work": key stored, SDK absent."""
    from atif_view import ai, config

    config.set_api_key("sk-ant-api03-" + "q" * 40)
    monkeypatch.setattr(
        ai, "_client", lambda: (_ for _ in ()).throw(ai.Unavailable("no anthropic package"))
    )
    state = _index(server)["ai"]
    assert state["available"] is False
    assert state["source"] == "settings", "the saved key must still be reported"
    assert state["hint"] == "…qqqq"
    assert "no anthropic package" in state["reason"]


def test_ai_is_off_when_nothing_is_configured(server, monkeypatch):
    from atif_view import ai, config

    monkeypatch.setattr(ai, "_client", _raise_unavailable)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert config.api_key() is None
    state = _index(server)["ai"]
    assert state["available"] is False and state["hint"] == ""
    assert state["reason"], "an unavailable state must say why"


def test_the_ai_endpoint_refuses_without_a_credential(server, monkeypatch):
    from atif_view import ai

    monkeypatch.setattr(ai, "_client", _raise_unavailable)
    status, payload = _try_post(server + "/api/ai", {"key": _first_key(server), "what": "ask"})
    assert status == 501
    assert "not configured" in payload["error"]


def test_a_saved_key_is_never_returned_to_the_page(server, credentialed):
    secret = "sk-ant-api03-" + "z" * 40
    status, payload = _try_post(server + "/api/settings", {"api_key": secret})
    assert status == 200
    assert secret not in json.dumps(payload)
    assert payload["source"] == "settings" and payload["hint"] == "\u2026zzzz"
    assert secret not in json.dumps(_index(server))


def test_a_key_that_is_not_one_is_refused(server, credentialed):
    status, payload = _try_post(server + "/api/settings", {"api_key": "nope"})
    assert status == 400
    assert "does not look like" in payload["error"]


def test_a_saved_key_can_be_removed(server, credentialed):
    _try_post(server + "/api/settings", {"api_key": "sk-ant-api03-" + "z" * 40})
    status, payload = _try_post(server + "/api/settings", {"clear": True})
    assert status == 200
    assert payload["hint"] == ""


def test_switching_ai_off_for_a_transcript_refuses_at_the_server(server, credentialed):
    """Hiding the controls is not enough; the endpoint must refuse too."""
    key = _first_key(server)
    _try_post(server + "/api/library", {"key": key, "ai": False})
    assert _sessions(server)[0]["ai"] is False

    status, payload = _try_post(
        server + "/api/ai", {"key": key, "what": "ask", "question": "hi"}
    )
    assert status == 403
    assert "switched off" in payload["error"]


def test_switching_ai_back_on_restores_it(server, credentialed):
    key = _first_key(server)
    _try_post(server + "/api/library", {"key": key, "ai": False})
    _try_post(server + "/api/library", {"key": key, "ai": True})
    assert _sessions(server)[0]["ai"] is True


# ---- streaming ------------------------------------------------------------------


def _ai_call_id(server: str) -> str:
    """A tool call id from the streaming fixture session."""
    key = _first_key(server)
    traj = json.loads(_get(server + f"/api/trajectory?id={key}")[1])
    for step in traj["steps"]:
        for call in step.get("tool_calls") or []:
            return call["tool_call_id"]
    pytest.fail("fixture has no tool call")


def _read_frames(url: str, body: dict, stop_after: int | None = None):
    """Read NDJSON frames as they arrive, recording when each one landed."""
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    frames, started = [], time.monotonic()
    with urllib.request.urlopen(request, timeout=20) as response:
        for line in response:
            if line.strip():
                frames.append((time.monotonic() - started, json.loads(line)))
                if stop_after and len(frames) >= stop_after:
                    break
    return frames


def test_an_answer_arrives_in_pieces_rather_than_all_at_once(server, credentialed):
    """The point of streaming: the first token lands long before the last."""
    from atif_view import ai

    def slow(*a, **k):
        for piece in ["one ", "two ", "three ", "four"]:
            time.sleep(0.15)
            yield piece

    ai_stream = pytest.MonkeyPatch()
    ai_stream.setattr(ai, "_stream", slow)
    try:
        frames = _read_frames(
            server + "/api/ai",
            {"key": _first_key(server), "what": "ask", "question": "what happened"},
        )
    finally:
        ai_stream.undo()

    deltas = [(at, f) for at, f in frames if f["t"] == "delta"]
    assert len(deltas) == 4, f"expected four deltas, got {[f for _, f in frames]}"
    assert deltas[0][0] < deltas[-1][0] - 0.2, "every frame arrived at once — not streamed"
    assert frames[0][1]["t"] == "steps", "the steps should be known before any text"
    assert frames[-1][1]["t"] == "done"
    assert "".join(f["text"] for _, f in deltas) == "one two three four"


def test_a_summary_is_streamed_then_kept(server, credentialed):
    from atif_view import ai

    patch = pytest.MonkeyPatch()
    patch.setattr(ai, "_stream", lambda *a, **k: iter(["It ", "listed ", "files."]))
    try:
        call_id = _ai_call_id(server)
        frames = _read_frames(server + "/api/ai", {
            "key": _first_key(server), "what": "call", "call_id": call_id,
        })
        assert [f["t"] for _, f in frames] == ["delta", "delta", "delta", "done"]

        # Paid for once: the second read must not touch the model.
        patch.setattr(ai, "_stream", _never_called)
        again = _read_frames(server + "/api/ai", {
            "key": _first_key(server), "what": "call", "call_id": call_id,
        })
    finally:
        patch.undo()

    assert "".join(f["text"] for _, f in again if f["t"] == "delta") == "It listed files."


def _never_called(*a, **k):
    raise AssertionError("a cached summary was regenerated")


def test_an_unknown_call_is_refused_before_the_stream_opens(server, credentialed):
    status, payload = _try_post(
        server + "/api/ai",
        {"key": _first_key(server), "what": "call", "call_id": "nope"},
    )
    assert status == 404 and payload["error"] == "no such call"
