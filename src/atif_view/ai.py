"""Optional Claude-backed help: explain one tool call, or answer a question
about a transcript.

Nothing here runs unless asked. A transcript carries source code, file contents
and tool output, so no call is made in the background, on load, or ahead of a
click — the viewer is local-only until you press a button.

The `anthropic` SDK is an optional extra (`pip install "atif-view[ai]"`) so the
package keeps its zero-dependency install; these features simply hide when it,
or a credential, is absent.
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import config

MODEL = "claude-opus-5"

# A whole transcript can run to millions of tokens — one here is 7.6M against a
# 1M window — so a question is answered from the steps that look relevant
# rather than the lot.
ASK_BUDGET_CHARS = 300_000
ASK_STEPS = 40


class Unavailable(RuntimeError):
    """No SDK, or no credential. Both mean the same thing to a caller."""


def _client():
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - depends on the extra
        raise Unavailable(
            'The anthropic package is not installed: uv tool install "atif-view[ai]"'
        ) from exc
    try:
        # A key from settings, else the SDK's own resolution: ANTHROPIC_API_KEY,
        # ANTHROPIC_AUTH_TOKEN, or an `ant auth login` profile. Never a key sent
        # up by the page — it only ever travels from the browser to storage.
        stored = config.api_key()
        return anthropic.Anthropic(api_key=stored) if stored else anthropic.Anthropic()
    except Exception as exc:
        raise Unavailable(
            "No Anthropic credential found. Add a key here, or set ANTHROPIC_API_KEY."
        ) from exc


def status() -> tuple[bool, str]:
    """Whether AI can run, and if not, what is missing.

    A bare boolean was not enough: a saved key with no SDK installed looks
    exactly like no key at all, which reads as "the save didn't work".
    """
    try:
        _client()
    except Unavailable as exc:
        return False, str(exc)
    return True, ""


def available() -> bool:
    """Whether the controls should be offered at all."""
    return status()[0]


def _say(client, system: str, prompt: str, max_tokens: int) -> str:
    """One request, adaptive thinking, streamed.

    Streaming keeps a long answer from hitting the request timeout; the SDK
    assembles the final message either way.
    """
    import anthropic

    try:
        with client.messages.stream(
            model=MODEL,
            max_tokens=max_tokens,
            system=system,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            message = stream.get_final_message()
    except anthropic.NotFoundError as exc:
        raise Unavailable(f"Model {MODEL} is not available to this account.") from exc
    except anthropic.RateLimitError as exc:
        raise Unavailable("Rate limited by the API — try again shortly.") from exc
    except anthropic.APIStatusError as exc:
        raise Unavailable(f"The API refused that request: {exc.status_code}") from exc
    except anthropic.APIConnectionError as exc:
        raise Unavailable("Could not reach the API.") from exc

    if message.stop_reason == "refusal":
        raise Unavailable("The model declined to answer that.")
    return "".join(b.text for b in message.content if b.type == "text").strip()


def _clip(value: Any, limit: int = 4000) -> str:
    text = value if isinstance(value, str) else json.dumps(value, indent=2)[:limit]
    return text[:limit]


# ---------------------------------------------------------------- one call ---

CALL_SYSTEM = """You explain a single step from an agent transcript to someone \
reviewing it later.

Two or three sentences. Say what the call was trying to do, and what actually \
came back — including whether it failed. Lead with the outcome. No preamble, no \
restating the command verbatim, no markdown headings."""


def summarise_call(call: dict, output: Any = None) -> str:
    """Explain one tool call and its result."""
    client = _client()
    parts = [
        f"Tool: {call.get('function_name', 'unknown')}",
        f"Arguments:\n{_clip(call.get('arguments', {}))}",
    ]
    if output:
        parts.append(f"Output:\n{_clip(output, 6000)}")
    else:
        parts.append("Output: (none recorded)")
    return _say(client, CALL_SYSTEM, "\n\n".join(parts), max_tokens=1000)


# ------------------------------------------------------------------- ask -----

ASK_SYSTEM = """You answer questions about an agent transcript, for someone \
reviewing what happened.

Answer only from the steps given. Cite the step numbers you used, as (step 42). \
If the steps do not contain the answer, say so plainly rather than guessing — \
they are a relevant-looking subset, not the whole transcript, so "it is not in \
what I was shown" is a useful answer.

Be direct and concrete. No preamble."""


def _text_of(step: dict) -> str:
    """Everything in a step a reader could have seen."""
    parts: list[str] = []
    message = step.get("message")
    if isinstance(message, str):
        parts.append(message)
    elif isinstance(message, list):
        parts += [p.get("text", "") for p in message if isinstance(p, dict)]
    if step.get("reasoning_content"):
        parts.append(step["reasoning_content"])
    for call in step.get("tool_calls") or []:
        parts.append(call.get("function_name", ""))
        parts.append(json.dumps(call.get("arguments", {})))
    for result in (step.get("observation") or {}).get("results") or []:
        content = result.get("content")
        if isinstance(content, str):
            parts.append(content)
    return "\n".join(p for p in parts if p)


def relevant_steps(
    question: str, steps: list[dict], limit: int = ASK_STEPS
) -> list[dict]:
    """The steps most likely to bear on the question.

    Deliberately simple: score by how many of the question's words a step
    mentions, preferring longer words, then keep document order so the answer
    reads chronologically. No embeddings, no index to maintain, and it degrades
    to "the last N steps" when nothing matches.
    """
    words = {w for w in re.findall(r"[a-zA-Z_][\w./-]{2,}", question.lower())}
    if not words:
        return steps[-limit:]

    # A question says "authentication" where the transcript says "test_auth.py",
    # so a long word also scores, at a discount, on its first four characters.
    # Cheaper than tokenising every step, and wrong guesses only mis-rank.
    stems = {w: w[:4] for w in words if len(w) > 6}

    scored = []
    for step in steps:
        haystack = _text_of(step).lower()
        score = 0
        for word in words:
            if word in haystack:
                score += len(word)
            elif word in stems and stems[word] in haystack:
                score += 2
        if score:
            scored.append((score, step.get("step_id", 0), step))

    scored.sort(key=lambda row: (-row[0], row[1]))
    chosen = [row[2] for row in scored[:limit]] or steps[-limit:]
    return sorted(chosen, key=lambda s: s.get("step_id", 0))


def ask(question: str, steps: list[dict]) -> tuple[str, list[int]]:
    """Answer a question about a transcript. Returns the answer and the steps used."""
    client = _client()
    chosen = relevant_steps(question, steps)

    rendered: list[str] = []
    used: list[int] = []
    budget = ASK_BUDGET_CHARS
    for step in chosen:
        body = _text_of(step)[:8000]
        block = f"--- step {step.get('step_id')} ({step.get('source')}) ---\n{body}"
        if len(block) > budget:
            break
        budget -= len(block)
        rendered.append(block)
        used.append(step.get("step_id", 0))

    prompt = f"Question: {question}\n\nSteps:\n\n" + "\n\n".join(rendered)
    return _say(client, ASK_SYSTEM, prompt, max_tokens=4000), used
