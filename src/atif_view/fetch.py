"""Pull trajectories from Hugging Face and GitHub by URL.

Two rules shape this module.

**Private address space is unreachable.** The page hands a URL to a server
running as you, on your network, so an unguarded fetcher would let it reach your
router, a cloud metadata endpoint, or a service bound to localhost. Any URL is
allowed, but the host is resolved first and refused if it lands on a loopback,
link-local, private or otherwise reserved address — checked again after
redirects, since a redirect is a second request to a second host.

Hugging Face and GitHub are understood well enough to list a repository; every
other host is treated as a direct link to one file.

**Nothing is downloaded without being listed first.** A dataset URL can name
hundreds of files; `plan()` says what would be fetched and how much it weighs,
and `download()` only runs once that has been seen.

Uses urllib rather than an SDK, so the viewer keeps its dependency-free install.
"""

from __future__ import annotations

import json
import re
import ipaddress
import socket
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import quote, urlparse

# Hosts whose layout is understood well enough to list a repository. Anything
# else is fetched as a single file.
HOSTS = {
    "huggingface.co": "hf",
    "cdn-lfs.huggingface.co": "hf",
    "github.com": "github",
    "api.github.com": "github",
    "raw.githubusercontent.com": "github",
}

# What atif-make can convert. Matches corpus.scan so a URL and a directory
# behave the same.
SUFFIXES = (".jsonl", ".json", ".har")

# Downloads land beside where the viewer was launched, so they are visible and
# usable by other tools rather than buried in a dot-directory.
DEFAULT_DIR = "atif-downloads"

# Somewhere a download must never be pointed at, however the path was typed.
# Deliberately not /var: macOS puts every temporary directory under
# /private/var/folders, so banning it refuses ordinary scratch paths.
PROTECTED = (
    "/bin", "/sbin", "/usr", "/etc", "/dev", "/System", "/Library", "/private/etc",
)

MAX_FILES = 2_000
MAX_TOTAL_BYTES = 2 * 1024**3
TIMEOUT = 60


class FetchError(RuntimeError):
    """Anything the person who pasted the URL needs to know about."""


class Plan(NamedTuple):
    """What a URL would bring in."""

    service: str
    label: str
    files: list["Remote"]
    web: str  # where to browse this on the site, "" if there is nowhere


@dataclass(frozen=True)
class Remote:
    """One file to fetch."""

    name: str  # path within the repo, used as the local name
    url: str
    size: int | None = None


def _check_host(url: str) -> str:
    """Refuse anything that resolves into private address space.

    An allowlist was simpler but ruled out every other host; this is the usual
    mitigation and lets a bare link work. It resolves the name and inspects the
    addresses rather than pattern-matching the text, so `127.0.0.1`,
    `localhost`, `0x7f.1`, and a public name pointed at a private address are
    all caught the same way.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise FetchError("Only https URLs are fetched.")

    host = parsed.hostname
    if not host:
        raise FetchError("That URL has no host.")

    try:
        addresses = {
            info[4][0] for info in socket.getaddrinfo(host, parsed.port or 443)
        }
    except socket.gaierror as exc:
        raise FetchError(f"Could not look up {host}.") from exc

    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if not ip.is_global or ip.is_multicast:
            raise FetchError(
                f"{host} resolves to {ip}, which is on this machine or this "
                f"network. Only public addresses are fetched."
            )

    return HOSTS.get(parsed.netloc, "direct")


def _open(url: str, token: str | None, service: str) -> Any:
    """A GET with the right auth header, refusing a redirect off the allowlist."""
    _check_host(url)
    request = urllib.request.Request(url)
    if token:
        # In a header, never in the URL: a URL reaches logs and history.
        request.add_header("Authorization", f"Bearer {token}")
    request.add_header("User-Agent", "atif-view")
    try:
        response = urllib.request.urlopen(request, timeout=TIMEOUT)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            where = "Hugging Face" if service == "hf" else "GitHub"
            raise FetchError(
                f"{where} refused that ({exc.code}). "
                f"A gated dataset needs a token in Settings, and its conditions "
                f"accepted on its page first."
            ) from exc
        if exc.code == 404:
            raise FetchError("Nothing there — check the URL.") from exc
        raise FetchError(f"{urlparse(url).netloc} said {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise FetchError(f"Could not reach {urlparse(url).netloc}.") from exc

    # urllib follows redirects itself, so verify where it actually landed.
    _check_host(response.geturl())
    return response


def _json(url: str, token: str | None, service: str) -> Any:
    with _open(url, token, service) as response:
        return json.loads(response.read())


# ------------------------------------------------------------- hugging face ---

_HF = re.compile(
    r"^/(?:(?P<kind>datasets|spaces)/)?(?P<repo>[^/]+/[^/]+)"
    r"(?:/(?P<action>tree|blob|resolve)/(?P<rev>[^/]+)(?P<path>/.*)?)?/?$"
)


def _hf_plan(url: str, token: str | None) -> tuple[str, list[Remote], str]:
    parsed = urlparse(url)
    match = _HF.match(parsed.path)
    if not match:
        raise FetchError("That does not look like a Hugging Face repo or file URL.")

    repo = match.group("repo")
    kind = match.group("kind") or "models"
    rev = match.group("rev") or "main"
    inner = (match.group("path") or "").strip("/")
    api_kind = {"datasets": "datasets", "spaces": "spaces", "models": "models"}[kind]
    prefix = f"{kind}/" if match.group("kind") else ""

    def raw(path: str) -> str:
        return f"https://huggingface.co/{prefix}{repo}/resolve/{rev}/{quote(path)}"

    web = f"https://huggingface.co/{prefix}{repo}/tree/{rev}"
    if match.group("action") == "blob" or (inner and inner.endswith(SUFFIXES)):
        return repo.replace("/", "--"), [
            Remote(name=inner.split("/")[-1], url=raw(inner))
        ], web

    listing = _json(
        f"https://huggingface.co/api/{api_kind}/{repo}/tree/{rev}/{quote(inner)}"
        f"?recursive=true",
        token,
        "hf",
    )
    files = [
        Remote(name=row["path"], url=raw(row["path"]), size=row.get("size"))
        for row in listing
        if row.get("type") == "file" and str(row.get("path", "")).endswith(SUFFIXES)
    ]
    return repo.replace("/", "--"), files, web


# ------------------------------------------------------------------ github ---

_GH = re.compile(
    r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)"
    r"(?:/(?P<action>tree|blob|raw)/(?P<rev>[^/]+)(?P<path>/.*)?)?/?$"
)


def _github_plan(url: str, token: str | None) -> tuple[str, list[Remote], str]:
    parsed = urlparse(url)

    if parsed.netloc == "raw.githubusercontent.com":
        name = parsed.path.strip("/").split("/")[-1]
        return "github", [Remote(name=name, url=url)], ""

    match = _GH.match(parsed.path)
    if not match:
        raise FetchError("That does not look like a GitHub repo or file URL.")

    owner, repo = match.group("owner"), match.group("repo").removesuffix(".git")
    rev = match.group("rev")
    inner = (match.group("path") or "").strip("/")

    if rev is None:
        info = _json(f"https://api.github.com/repos/{owner}/{repo}", token, "github")
        rev = info.get("default_branch") or "main"

    def raw(path: str) -> str:
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{rev}/{quote(path)}"

    web = f"https://github.com/{owner}/{repo}/tree/{rev}"
    if match.group("action") in ("blob", "raw"):
        return f"{owner}--{repo}", [
            Remote(name=inner.split("/")[-1], url=raw(inner))
        ], web

    tree = _json(
        f"https://api.github.com/repos/{owner}/{repo}/git/trees/{rev}?recursive=1",
        token,
        "github",
    )
    if tree.get("truncated"):
        raise FetchError(
            "That repository is too large to list in one request. "
            "Link a subdirectory instead."
        )
    files = [
        Remote(name=row["path"], url=raw(row["path"]), size=row.get("size"))
        for row in tree.get("tree", [])
        if row.get("type") == "blob"
        and str(row.get("path", "")).endswith(SUFFIXES)
        and (not inner or str(row.get("path", "")).startswith(f"{inner}/"))
    ]
    return f"{owner}--{repo}", files, web


# -------------------------------------------------------------------- api ----


def destination(raw: str | None) -> Path:
    """Resolve where a download should land.

    The path comes from the page, so it is resolved before it is trusted: `..`
    and a symlink both collapse here, and a handful of places nothing should
    ever be written into are refused outright.
    """
    text = (raw or "").strip() or DEFAULT_DIR
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()

    home = Path.home().resolve()
    if (
        path == Path(path.anchor)
        or path == home
        or path in home.parents
        or any(path == Path(p) or Path(p) in path.parents for p in PROTECTED)
    ):
        raise FetchError(f"{path} is not somewhere to download into — pick a folder.")
    return path


def plan(url: str, tokens: dict[str, str | None]) -> Plan:
    """What fetching this URL would pull: the service, a label, and the files."""
    url = (url or "").strip()
    if not url:
        raise FetchError("No URL given.")
    service = _check_host(url)
    token = tokens.get(service)

    if service == "direct":
        name = Path(urlparse(url).path).name or "download.jsonl"
        if not name.endswith(SUFFIXES):
            raise FetchError(
                f"{name} is not a kind this reads — looking for "
                f"{', '.join(SUFFIXES)}."
            )
        label, files, web = urlparse(url).hostname or "download", [
            Remote(name=name, url=url)
        ], ""
    else:
        label, files, web = (_hf_plan if service == "hf" else _github_plan)(url, token)
    if not files:
        raise FetchError(
            "Nothing convertible there — looking for .jsonl, .json or .har files."
        )
    if len(files) > MAX_FILES:
        raise FetchError(
            f"{len(files):,} files is more than this fetches at once "
            f"(limit {MAX_FILES:,}). Link a subdirectory."
        )
    known = sum(f.size or 0 for f in files)
    if known > MAX_TOTAL_BYTES:
        raise FetchError(
            f"That is {known / 1024**3:.1f} GB, over the {MAX_TOTAL_BYTES // 1024**3} GB limit."
        )
    return Plan(service, label, files, web)


def download(
    service: str, files: list[Remote], into: Path, tokens: dict[str, str | None]
) -> Iterator[Path]:
    """Fetch each file under ``into``, yielding each as it lands.

    A generator so a caller can report progress: a dataset of several hundred
    files takes long enough that a still screen reads as a hang.
    """
    token = tokens.get(service)
    total = 0

    for remote in files:
        # The name comes from a remote listing, so it is not trusted to stay
        # inside the directory.
        parts = [p for p in Path(remote.name).parts if p not in ("..", "/", "")]
        if not parts:
            continue
        target = into.joinpath(*parts)
        if not target.resolve().is_relative_to(into.resolve()):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)

        with _open(remote.url, token, service) as response:
            body = response.read(MAX_TOTAL_BYTES - total + 1)
        total += len(body)
        if total > MAX_TOTAL_BYTES:
            raise FetchError("That is larger than the fetch limit.")
        target.write_bytes(body)
        yield target
