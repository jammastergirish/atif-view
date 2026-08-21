"""The page's stylesheet, checked for the mistakes CSS makes silently.

A broken selector is not an error: the browser drops the rule and renders on.
Both faults these guard against shipped unnoticed — a header button styled as
bare text beside two pills, and a line whose newlines were the two characters
`\\n`, which killed the two rules after it.
"""

import re

import pytest

from atif_view.viewer import PAGE

STYLE = PAGE[PAGE.index("<style>") + 7 : PAGE.index("</style>")]


def test_the_header_controls_share_one_rule():
    """Three copies of a box is how the settings button came to look different."""
    match = re.search(r"^#theme,#gear,#open\{", STYLE, re.MULTILINE)
    assert match, "the header controls no longer share a rule"


@pytest.mark.parametrize("control", ["#theme", "#gear", "#open"])
def test_no_header_control_redefines_its_own_box(control):
    """A later rule of its own is exactly how one drifts away from the others."""
    own = re.findall(rf"^{re.escape(control)}\{{([^}}]*)\}}", STYLE, re.MULTILINE)
    for body in own:
        for prop in ("background", "border", "border-radius", "padding"):
            assert prop not in body, (
                f"{control} sets {prop} on its own; it belongs in the shared rule"
            )


def test_no_rule_is_killed_by_an_escaped_newline():
    r"""`\n` in a stylesheet is an escaped letter n, not a line break.

    The parser then treats what follows as part of a selector, drops the rule as
    invalid, and says nothing.
    """
    assert "\\n" not in STYLE, "a literal backslash-n is in the stylesheet"


def test_no_selector_sets_the_same_property_twice():
    """A second rule that adds properties is ordinary CSS; one that overrides the
    same property leaves a dead value behind, which is how a stale 320px sidebar
    width outlived the design that used it."""
    dead = []
    for name in set(re.findall(r"^([#.][\w-]+(?: [\w.#-]+)?)\{", STYLE, re.MULTILINE)):
        props: dict[str, int] = {}
        for body in re.findall(rf"^{re.escape(name)}\{{([^}}]*)\}}", STYLE, re.MULTILINE):
            for declaration in body.split(";"):
                if ":" not in declaration:
                    continue
                prop = declaration.split(":", 1)[0].strip()
                props[prop] = props.get(prop, 0) + 1
        dead += [f"{name} {{{p}}}" for p, count in props.items() if count > 1]
    assert not dead, f"set more than once, so an earlier value is dead: {sorted(dead)}"


def test_every_rule_closes():
    assert STYLE.count("{") == STYLE.count("}"), "an unbalanced brace in the stylesheet"


def test_colours_outside_the_themes_come_from_tokens():
    """Literal colours belong in the :root blocks that define a theme. Anywhere
    else they ignore whichever theme the reader chose."""
    without_themes = re.sub(r":root[^{]*\{[^}]*\}", "", STYLE)
    literals = re.findall(r":\s*(#[0-9a-fA-F]{3,8})\b", without_themes)
    assert not literals, f"hard-coded colours defeat the themes: {sorted(set(literals))}"
