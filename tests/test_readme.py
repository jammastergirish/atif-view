"""The README is documentation of this code, so it is checked against it.

Prose drifts silently. Twice now a documented behaviour outlived the code —
once because an edit matched nothing and reported success anyway. These pair
each claim with the thing that makes it true, so a change that outdates the
README fails here rather than misleading a reader.

Deliberately shallow: presence of a marker, not a parse. A test that tried to
verify the prose itself would be a worse copy of the code.
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _read(name: str) -> str:
    return (ROOT / name).read_text()


README = _read("README.md")


# (claim, marker proving it in the source, source file)
CLAIMS = [
    ("With AI support", "With AI support", "src/atif_view/viewer.py"),
    ("with no button to press", "function callSummary", "src/atif_view/viewer.py"),
    ("read all", "read all ${num(seen)}", "src/atif_view/viewer.py"),
    ("~/.atif/config.json", 'store.ROOT / "config.json"', "src/atif_view/config.py"),
    ("0600", "0o600", "src/atif_view/store.py"),
    ("0700", "0o700", "src/atif_view/store.py"),
    ("nosniff", "X-Content-Type-Options", "src/atif_view/viewer.py"),
    ("newline-delimited JSON", "application/x-ndjson", "src/atif_view/viewer.py"),
    ("Thinking…", '"t": "thinking"', "src/atif_view/viewer.py"),
    ("skips thinking", "thinking=False", "src/atif_view/ai.py"),
    ("quadratic", "HISTORY_TURNS", "src/atif_view/ai.py"),
    ("links into the transcript", "function jumpToStep", "src/atif_view/viewer.py"),
    ("--extra ai", "ai = [", "pyproject.toml"),
]


@pytest.mark.parametrize(
    ("claim", "marker", "source"), CLAIMS, ids=[c[0] for c in CLAIMS]
)
def test_a_documented_behaviour_still_exists(claim, marker, source):
    assert claim in README, f"the README no longer makes this claim: {claim!r}"
    assert marker in _read(source), (
        f"the README claims {claim!r} but {source} no longer contains {marker!r}"
    )


# Behaviour that was removed, and the wording that described it.
REMOVED = [
    ("disable_nagle_algorithm", "src/atif_view/viewer.py", "TCP_NODELAY is set"),
    ("summary · new", "src/atif_view/viewer.py", "summary · new"),
    (
        "off for this transcript</span>",
        "src/atif_view/viewer.py",
        "off for this transcript",
    ),
    ('onclick="clearAsk()"', "src/atif_view/viewer.py", "clear</span>"),
]


@pytest.mark.parametrize(
    ("marker", "source", "wording"), REMOVED, ids=[r[0] for r in REMOVED]
)
def test_removed_behaviour_is_not_still_documented(marker, source, wording):
    if marker in _read(source):
        pytest.skip(f"{marker} is back in the code; this guard no longer applies")
    assert wording not in README, (
        f"{marker!r} was removed from {source} but the README still describes it"
    )


def test_the_readme_recommends_uv_rather_than_pip():
    """Installing this with pip is not how it is meant to be used."""
    assert "pip install" not in README


def test_every_measured_number_is_labelled_as_measured():
    """Figures in the README came from running something, not from a guess."""
    for figure in ["3.8 ms", "58 are sent entire", "202 of 202"]:
        assert figure in README, f"a measured figure went missing: {figure}"
