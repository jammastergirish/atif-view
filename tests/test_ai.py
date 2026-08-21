"""The AI layer, tested without calling the API.

The model call itself is stubbed — what matters here is that nothing is sent
unasked, that a paid-for summary is not paid for twice, and that a question
against a transcript far larger than any context window still gets a sensible
subset of steps.
"""

from atif_view import ai


def _step(step_id, source="agent", message="", tool=None, output=None, reasoning=""):
    step = {"step_id": step_id, "source": source, "message": message}
    if reasoning:
        step["reasoning_content"] = reasoning
    if tool:
        step["tool_calls"] = [
            {
                "tool_call_id": f"c{step_id}",
                "function_name": tool[0],
                "arguments": tool[1],
            }
        ]
        if output is not None:
            step["observation"] = {
                "results": [{"source_call_id": f"c{step_id}", "content": output}]
            }
    return step


# ---- nothing runs without a credential ---------------------------------------


def test_unavailable_without_the_extra_or_a_credential(monkeypatch):
    def refuse():
        raise ai.Unavailable("nope")

    monkeypatch.setattr(ai, "_client", refuse)
    assert ai.available() is False


def test_available_when_a_client_can_be_built(monkeypatch):
    monkeypatch.setattr(ai, "_client", lambda: object())
    assert ai.available() is True


# ---- picking what to send -----------------------------------------------------


def test_relevant_steps_prefers_steps_that_mention_the_question():
    steps = [
        _step(1, message="setting up the project"),
        _step(2, message="editing the authentication middleware"),
        _step(3, message="running the formatter"),
        _step(4, tool=("Bash", {"command": "pytest tests/test_auth.py"})),
    ]
    picked = ai.relevant_steps("where did it change authentication?", steps, limit=2)
    assert [s["step_id"] for s in picked] == [2, 4]


def test_a_long_word_also_matches_on_its_stem():
    """A question says "authentication"; the transcript says "test_auth.py"."""
    steps = [_step(1, message="unrelated"), _step(2, message="ran test_auth.py")]
    assert ai.relevant_steps("authentication", steps, limit=1)[0]["step_id"] == 2


def test_a_stem_match_ranks_below_a_whole_word_match():
    steps = [_step(1, message="auth helper"), _step(2, message="authentication flow")]
    assert ai.relevant_steps("authentication", steps, limit=1)[0]["step_id"] == 2


def test_relevant_steps_keeps_document_order():
    """An answer that jumps around the transcript is harder to check."""
    steps = [_step(i, message="auth" if i % 2 else "other") for i in range(1, 11)]
    picked = ai.relevant_steps("auth", steps, limit=5)
    assert [s["step_id"] for s in picked] == sorted(s["step_id"] for s in picked)


def test_relevant_steps_falls_back_to_the_end_when_nothing_matches():
    steps = [_step(i, message="unrelated") for i in range(1, 21)]
    picked = ai.relevant_steps("quantum tunnelling", steps, limit=3)
    assert [s["step_id"] for s in picked] == [18, 19, 20]


def test_relevant_steps_handles_a_question_with_no_usable_words():
    steps = [_step(i) for i in range(1, 6)]
    assert len(ai.relevant_steps("?? !!", steps, limit=2)) == 2


def test_a_step_search_sees_tool_arguments_and_output():
    """What a reader can see, the search can see."""
    steps = [
        _step(1, message="nothing here"),
        _step(2, tool=("Bash", {"command": "grep -r needle src/"}), output="found it"),
    ]
    assert ai.relevant_steps("needle", steps, limit=1)[0]["step_id"] == 2
    assert ai.relevant_steps("found", steps, limit=1)[0]["step_id"] == 2


def test_ask_stays_inside_its_character_budget(monkeypatch):
    """A transcript can be far larger than any context window."""
    sent = {}

    def fake_say(client, system, prompt, max_tokens):
        sent["prompt"] = prompt
        return "an answer"

    monkeypatch.setattr(ai, "_client", lambda: object())
    monkeypatch.setattr(ai, "_say", fake_say)

    huge = [_step(i, message="auth " + "x" * 9000) for i in range(1, 400)]
    answer, used = ai.ask("auth", huge)

    assert answer == "an answer"
    assert len(sent["prompt"]) < ai.ASK_BUDGET_CHARS + 20_000
    assert used and len(used) <= ai.ASK_STEPS


def test_ask_reports_which_steps_it_used(monkeypatch):
    monkeypatch.setattr(ai, "_client", lambda: object())
    monkeypatch.setattr(ai, "_say", lambda *a, **k: "answer")
    steps = [
        _step(1, message="alpha"),
        _step(2, message="beta"),
        _step(3, message="beta"),
    ]
    _, used = ai.ask("beta", steps)
    assert used == [2, 3]


# ---- summarising one call ------------------------------------------------------


def test_summarise_call_sends_the_call_and_its_output(monkeypatch):
    seen = {}
    monkeypatch.setattr(ai, "_client", lambda: object())
    monkeypatch.setattr(
        ai,
        "_say",
        lambda client, system, prompt, max_tokens: seen.setdefault("p", prompt) or "ok",
    )
    ai.summarise_call(
        {"function_name": "Bash", "arguments": {"command": "ls -la"}}, "README.md"
    )
    assert "Bash" in seen["p"] and "ls -la" in seen["p"] and "README.md" in seen["p"]


def test_summarise_call_copes_with_no_output(monkeypatch):
    seen = {}
    monkeypatch.setattr(ai, "_client", lambda: object())
    monkeypatch.setattr(
        ai,
        "_say",
        lambda client, system, prompt, max_tokens: seen.setdefault("p", prompt) or "ok",
    )
    ai.summarise_call({"function_name": "Bash", "arguments": {}}, None)
    assert "none recorded" in seen["p"]


def test_large_output_is_clipped_rather_than_sent_whole(monkeypatch):
    seen = {}
    monkeypatch.setattr(ai, "_client", lambda: object())
    monkeypatch.setattr(
        ai,
        "_say",
        lambda client, system, prompt, max_tokens: seen.setdefault("p", prompt) or "ok",
    )
    ai.summarise_call({"function_name": "Bash", "arguments": {}}, "x" * 500_000)
    assert len(seen["p"]) < 20_000
