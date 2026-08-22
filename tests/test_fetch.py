"""Fetching trajectories by URL.

Nothing here reaches the network: `_open` is stubbed. What is tested is the part
that has to be right whether or not a host is reachable — that a URL the page
supplies cannot make the server talk to anything but the four allowed hosts, and
that a remote listing cannot write outside the directory it was given.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from atif_view import fetch


class _Response(io.BytesIO):
    def __init__(self, body: bytes, url: str):
        super().__init__(body)
        self._url = url

    def geturl(self) -> str:
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _stub(monkeypatch, routes: dict, landed: str | None = None):
    """Answer known URLs from a dict; record the auth header each call carried."""
    seen: dict = {"auth": [], "urls": []}

    def fake_open(url, token, service):
        fetch._check_host(url)
        seen["urls"].append(url)
        seen["auth"].append(token)
        for pattern, body in routes.items():
            if pattern in url:
                payload = body if isinstance(body, bytes) else json.dumps(body).encode()
                return _Response(payload, landed or url)
        raise fetch.FetchError(f"no stub for {url}")

    monkeypatch.setattr(fetch, "_open", fake_open)
    return seen


# ---- what may be reached ---------------------------------------------------------


@pytest.fixture(autouse=True)
def dns(monkeypatch):
    """Resolve names without touching the network.

    Anything not named here resolves to a public address, so a test that cares
    about address space says so explicitly.
    """
    table = {
        "localhost": "127.0.0.1",
        "127.0.0.1": "127.0.0.1",
        "169.254.169.254": "169.254.169.254",
        "192.168.1.1": "192.168.1.1",
        "10.0.0.5": "10.0.0.5",
        "internal.example": "172.16.4.4",
        "nowhere.example": None,
    }

    def resolve(host, port, *args, **kwargs):
        address = table.get(host, "93.184.216.34")
        if address is None:
            raise fetch.socket.gaierror("no such host")
        return [(2, 1, 6, "", (address, port or 443))]

    monkeypatch.setattr(fetch.socket, "getaddrinfo", resolve)


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost/x.jsonl",
        "https://127.0.0.1:8080/admin.jsonl",
        "https://169.254.169.254/latest/meta-data.json",
        "https://192.168.1.1/admin.jsonl",
        "https://10.0.0.5/x.jsonl",
        "https://internal.example/x.jsonl",
    ],
    ids=["localhost", "loopback", "cloud-metadata", "router", "private-10", "public-name"],
)
def test_an_address_on_this_machine_or_network_is_refused(url):
    """The page hands this to a server on your network; it must not be a proxy.

    The check is on the resolved address, not the text, so a perfectly ordinary
    hostname pointed at a private address is caught too.
    """
    with pytest.raises(fetch.FetchError, match="Only public addresses"):
        fetch.plan(url, {})


@pytest.mark.parametrize(
    "url",
    ["http://huggingface.co/datasets/a/b", "file:///etc/passwd", "ftp://example.com/x"],
    ids=["plain-http", "file", "ftp"],
)
def test_only_https_is_fetched(url):
    with pytest.raises(fetch.FetchError, match="https"):
        fetch.plan(url, {})


def test_a_host_that_does_not_resolve_is_reported():
    with pytest.raises(fetch.FetchError, match="Could not look up"):
        fetch.plan("https://nowhere.example/x.jsonl", {})


def test_an_empty_url_is_refused():
    with pytest.raises(fetch.FetchError, match="No URL"):
        fetch.plan("  ", {})


@pytest.mark.parametrize(
    ("url", "service"),
    [
        ("https://huggingface.co/datasets/a/b", "hf"),
        ("https://github.com/a/b", "github"),
        ("https://example.com/logs/a.jsonl", "direct"),
    ],
)
def test_a_public_host_is_classified_by_what_is_understood(url, service):
    assert fetch._check_host(url) == service


def test_a_bare_url_is_taken_as_one_file():
    """Only two hosts can be listed; everywhere else is a direct link."""
    _, label, files = fetch.plan("https://example.com/runs/session.jsonl", {})
    assert label == "example.com"
    assert [f.name for f in files] == ["session.jsonl"]


def test_a_bare_url_to_something_unreadable_says_so():
    with pytest.raises(fetch.FetchError, match="not a kind this reads"):
        fetch.plan("https://example.com/notes.pdf", {})


def test_a_redirect_into_private_space_is_refused(monkeypatch):
    """A redirect is a second request to a second host, so it is checked again."""

    def landed_private(request, timeout=None):
        return _Response(b"{}", "https://192.168.1.1/x.jsonl")

    monkeypatch.setattr(fetch.urllib.request, "urlopen", landed_private)
    with pytest.raises(fetch.FetchError, match="Only public addresses"):
        fetch._open("https://example.com/x.jsonl", None, "direct")


def test_a_redirect_between_public_hosts_is_fine(monkeypatch):
    def landed_public(request, timeout=None):
        return _Response(b"{}", "https://cdn.example.com/blob")

    monkeypatch.setattr(fetch.urllib.request, "urlopen", landed_public)
    with fetch._open("https://example.com/x.jsonl", None, "direct") as response:
        assert response.read() == b"{}"


def test_a_token_travels_in_a_header_never_in_the_url(monkeypatch):
    """A URL reaches logs, history and error messages; a header does not."""
    captured = {}

    def capture(request, timeout=None):
        captured["url"] = request.full_url
        captured["auth"] = request.get_header("Authorization")
        return _Response(b"{}", request.full_url)

    monkeypatch.setattr(fetch.urllib.request, "urlopen", capture)
    fetch._open("https://huggingface.co/api/datasets/a/b", "secret-token", "hf")
    assert captured["auth"] == "Bearer secret-token"
    assert "secret-token" not in captured["url"]


@pytest.mark.parametrize(
    ("code", "expected"),
    [(401, "needs a token"), (403, "needs a token"), (404, "Nothing there"),
     (500, "said 500")],
)
def test_an_http_failure_says_what_to_do_about_it(monkeypatch, code, expected):
    def refuse(request, timeout=None):
        raise fetch.urllib.error.HTTPError(request.full_url, code, "no", {}, None)

    monkeypatch.setattr(fetch.urllib.request, "urlopen", refuse)
    with pytest.raises(fetch.FetchError, match=expected):
        fetch._open("https://huggingface.co/x", None, "hf")


# ---- hugging face ----------------------------------------------------------------


def test_a_dataset_url_lists_its_convertible_files(monkeypatch):
    seen = _stub(
        monkeypatch,
        {
            "api/datasets": [
                {"type": "file", "path": "a/transcript.jsonl", "size": 10},
                {"type": "file", "path": "a/description.md", "size": 5},
                {"type": "directory", "path": "a"},
                {"type": "file", "path": "b/metadata.json", "size": 7},
            ]
        },
    )
    service, label, files = fetch.plan(
        "https://huggingface.co/datasets/owner/name", {"hf": "tok"}
    )
    assert service == "hf"
    assert label == "owner--name"
    assert [f.name for f in files] == ["a/transcript.jsonl", "b/metadata.json"]
    assert all("/resolve/main/" in f.url for f in files)
    assert seen["auth"] == ["tok"], "the token was not sent"


def test_a_tree_url_with_a_revision_and_subdirectory(monkeypatch):
    seen = _stub(monkeypatch, {"api/datasets": [{"type": "file", "path": "x.jsonl"}]})
    fetch.plan(
        "https://huggingface.co/datasets/owner/name/tree/v2/attacks/one", {"hf": None}
    )
    assert "/tree/v2/attacks/one" in seen["urls"][0]


def test_a_single_file_url_needs_no_listing(monkeypatch):
    seen = _stub(monkeypatch, {})
    _, _, files = fetch.plan(
        "https://huggingface.co/datasets/owner/name/blob/main/a/transcript.jsonl", {}
    )
    assert [f.name for f in files] == ["transcript.jsonl"]
    assert files[0].url.endswith("/resolve/main/a/transcript.jsonl")
    assert seen["urls"] == [], "a single file should not need the tree API"


def test_a_model_repo_works_as_well_as_a_dataset(monkeypatch):
    seen = _stub(monkeypatch, {"api/models": [{"type": "file", "path": "t.jsonl"}]})
    _, label, files = fetch.plan("https://huggingface.co/owner/name", {})
    assert label == "owner--name"
    assert "api/models/owner/name" in seen["urls"][0]


def test_a_url_that_is_not_a_repo_is_refused():
    with pytest.raises(fetch.FetchError, match="does not look like"):
        fetch.plan("https://huggingface.co/", {})


# ---- github ----------------------------------------------------------------------


def test_a_repo_url_uses_the_default_branch(monkeypatch):
    seen = _stub(
        monkeypatch,
        {
            # More specific first: the repo-info URL is a prefix of the tree URL.
            "git/trees": {"tree": [{"type": "blob", "path": "logs/a.jsonl"}]},
            "api.github.com/repos/owner/name": {"default_branch": "trunk"},
        },
    )
    _, label, files = fetch.plan("https://github.com/owner/name", {"github": "gh"})
    assert label == "owner--name"
    assert "/trunk/" in files[0].url
    assert seen["auth"] == ["gh", "gh"]


def test_a_subdirectory_narrows_the_listing(monkeypatch):
    _stub(
        monkeypatch,
        {
            "git/trees": {
                "tree": [
                    {"type": "blob", "path": "logs/a.jsonl"},
                    {"type": "blob", "path": "other/b.jsonl"},
                    {"type": "tree", "path": "logs"},
                ]
            }
        },
    )
    _, _, files = fetch.plan("https://github.com/owner/name/tree/main/logs", {})
    assert [f.name for f in files] == ["logs/a.jsonl"]


def test_a_truncated_tree_is_reported_rather_than_silently_partial(monkeypatch):
    _stub(monkeypatch, {"git/trees": {"tree": [], "truncated": True}})
    with pytest.raises(fetch.FetchError, match="too large to list"):
        fetch.plan("https://github.com/owner/name/tree/main", {})


def test_a_raw_url_is_taken_as_one_file(monkeypatch):
    seen = _stub(monkeypatch, {})
    _, _, files = fetch.plan(
        "https://raw.githubusercontent.com/owner/name/main/logs/a.jsonl", {}
    )
    assert [f.name for f in files] == ["a.jsonl"]
    assert seen["urls"] == []


def test_a_blob_url_is_rewritten_to_raw(monkeypatch):
    _stub(monkeypatch, {})
    _, _, files = fetch.plan("https://github.com/owner/name/blob/main/a.jsonl", {})
    assert files[0].url.startswith("https://raw.githubusercontent.com/")


# ---- limits ----------------------------------------------------------------------


def test_nothing_convertible_is_said_plainly(monkeypatch):
    _stub(monkeypatch, {"api/datasets": [{"type": "file", "path": "readme.md"}]})
    with pytest.raises(fetch.FetchError, match="Nothing convertible"):
        fetch.plan("https://huggingface.co/datasets/owner/name", {})


def test_too_many_files_is_refused_before_downloading(monkeypatch):
    _stub(
        monkeypatch,
        {
            "api/datasets": [
                {"type": "file", "path": f"{i}.jsonl"}
                for i in range(fetch.MAX_FILES + 1)
            ]
        },
    )
    with pytest.raises(fetch.FetchError, match="more than this fetches"):
        fetch.plan("https://huggingface.co/datasets/owner/name", {})


def test_too_many_bytes_is_refused_before_downloading(monkeypatch):
    _stub(
        monkeypatch,
        {
            "api/datasets": [
                {"type": "file", "path": "big.jsonl", "size": fetch.MAX_TOTAL_BYTES + 1}
            ]
        },
    )
    with pytest.raises(fetch.FetchError, match="over the"):
        fetch.plan("https://huggingface.co/datasets/owner/name", {})


# ---- downloading -----------------------------------------------------------------


def test_files_land_under_the_directory_given(monkeypatch, tmp_path):
    _stub(monkeypatch, {"a.jsonl": b'{"role":"user"}'})
    written = list(
        fetch.download(
            "hf",
            [fetch.Remote("logs/a.jsonl", "https://huggingface.co/a.jsonl")],
            tmp_path,
            {},
        )
    )
    assert written == [tmp_path / "logs" / "a.jsonl"]
    assert written[0].read_bytes() == b'{"role":"user"}'


@pytest.mark.parametrize(
    "name",
    ["../escape.jsonl", "../../etc/passwd", "a/../../out.jsonl", "/absolute.jsonl"],
    ids=["parent", "deep-parent", "mid-parent", "absolute"],
)
def test_a_remote_name_cannot_escape_the_directory(monkeypatch, tmp_path, name):
    """The listing is remote data; a crafted path must not write outside."""
    _stub(monkeypatch, {"x": b"{}"})
    written = list(
        fetch.download("hf", [fetch.Remote(name, "https://huggingface.co/x")], tmp_path, {})
    )
    for path in written:
        assert path.resolve().is_relative_to(tmp_path.resolve()), f"escaped: {path}"


# ---- where a download lands --------------------------------------------------------


def test_the_default_is_beside_where_the_viewer_was_launched(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert fetch.destination(None) == (tmp_path / fetch.DEFAULT_DIR).resolve()
    assert fetch.destination("  ") == (tmp_path / fetch.DEFAULT_DIR).resolve()


def test_a_relative_path_is_taken_from_the_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert fetch.destination("data/runs") == (tmp_path / "data" / "runs").resolve()


def test_a_tilde_is_expanded(tmp_path):
    assert fetch.destination("~/somewhere") == (Path.home() / "somewhere").resolve()


def test_dot_dot_collapses_before_the_path_is_used(tmp_path, monkeypatch):
    """The path arrives from the page, so it is resolved before it is trusted."""
    monkeypatch.chdir(tmp_path)
    assert ".." not in str(fetch.destination("a/../b"))


@pytest.mark.parametrize(
    "raw",
    ["/", "/usr", "/usr/local/lib", "/etc", "/System/Library", "~"],
    ids=["root", "usr", "under-usr", "etc", "system", "home-itself"],
)
def test_somewhere_nothing_should_be_written_is_refused(raw):
    with pytest.raises(fetch.FetchError, match="not somewhere to download into"):
        fetch.destination(raw)


def test_a_parent_of_home_is_refused():
    parent = str(Path.home().resolve().parent)
    with pytest.raises(fetch.FetchError):
        fetch.destination(parent)


def test_an_ordinary_folder_under_home_is_fine():
    target = fetch.destination("~/Documents/atif-downloads")
    assert target.is_relative_to(Path.home().resolve())
