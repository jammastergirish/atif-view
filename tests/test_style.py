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


HEADER_CONTROLS = ("#theme", "#gear", "#open")


def test_the_header_controls_share_one_rule():
    """Separate copies of a box is how the settings button came to look different."""
    shared = re.search(r"^(#[\w,#-]+)\{[^}]*border-radius:8px", STYLE, re.MULTILINE)
    assert shared, "no shared rule defines the header control box"
    named = set(shared.group(1).split(","))
    missing = [c for c in HEADER_CONTROLS if c not in named]
    assert not missing, f"not in the shared rule, so they will drift: {missing}"


@pytest.mark.parametrize("control", HEADER_CONTROLS)
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


def test_a_checkbox_is_not_styled_as_a_text_field():
    """`.sheet input {width:100%}` caught checkboxes and swallowed whole rows."""
    for match in re.finditer(r"^\.sheet input([^{]*)\{([^}]*)\}", STYLE, re.MULTILINE):
        qualifier, body = match.group(1), match.group(2)
        if "checkbox" in qualifier:
            continue
        assert "not([type=checkbox])" in qualifier or "width:100%" not in body, (
            f".sheet input{qualifier} sets width on every input, checkboxes included"
        )


def test_the_picker_rows_lay_out_left_to_right():
    assert re.search(r"^\.pkrow\{[^}]*display:flex", STYLE, re.MULTILINE)
    assert re.search(r"^\.pkname\{[^}]*flex:1", STYLE, re.MULTILINE)


def test_no_template_expression_is_left_in_the_static_markup():
    """The modal's HTML is not a template literal: a ${…} there renders as text,
    which is how the examples row shipped showing its own source."""
    from atif_view.viewer import PAGE

    markup = PAGE[: PAGE.index("<script>")]
    assert "${" not in markup, "a JS template expression is sitting in static HTML"
