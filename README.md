# atif-viewer

Browse [ATIF v1.7](https://github.com/harbor-framework/harbor/blob/main/rfcs/0001-trajectory-format.md)
trajectories in a local web viewer.

Conversion lives in [`atif-make`](https://github.com/jammastergirish/atif-make); this package depends on it and
adds only the browser interface.

## Install

```sh
uv tool install atif-viewer        # pulls atif-make automatically
```

```sh
atif-viewer                        # everything in the atif-make index
atif-viewer path/to/session.jsonl  # one log
atif-viewer bundle.zip             # a bundle someone sent you
atif-viewer --port 8080 --no-open
```

Build the index first with `atif-make index`, or point the viewer at a file,
directory or archive and it will scan that instead.

## What it shows

```sh
atif-viewer view              # everything in the index
atif-viewer view path/to/log  # a single file
atif-viewer view --port 8080 --no-open
```

**Stack**: Python's standard-library `http.server` and a single self-contained
HTML page — vanilla JS and CSS, no framework, no build step, no CDN. That is the
whole point of the zero-dependency rule: the viewer is one file you can read.

Binds `127.0.0.1` only — session logs routinely contain source code and tool
output, and must not be reachable off-host. Trajectories convert on demand and
cache in memory, so opening a large corpus is cheap.

**Links.** URLs and absolute filesystem paths are both linkified in one pass —
in prose, inside JSON argument values, and in tool output. Clicking a path
reveals it in Finder (`open -R`, which selects the item rather than launching
it, so clicking a path in a log can never execute anything); a path that no
longer exists is struck through instead. `file://` links cannot be used for this
because Chrome refuses to follow them from an http page, so the link calls back
to the local server. Path detection requires a plausible root (`~`, `/Users`,
`/opt`, …) so prose like "and/or", "3/4" and "2026/08/20" is left alone; inside
a JSON string the quotes bound the value, so paths with spaces work there.

Code spans and fenced blocks are linkified too. Standard Markdown leaves them
literal, but in agent transcripts a path or URL is usually written in backticks
— on one real corpus, 131 of 181 linkable targets sat inside code, so honouring
the convention would have hidden most of them.

**Markdown.** Agent messages are written in Markdown — headings, lists, code
fences, tables — so the viewer renders them as such. The renderer is ~60 lines
of vanilla JS inlined in the page: it escapes the source *first* and only then
applies transforms, so no markup from a log can reach the DOM, and only
`http(s)` links become anchors. A `raw text` toggle shows the unrendered string
when you need to see exactly what the model emitted.

Images are served from memory at `/api/image` and rendered inline, so a session
with screenshots is browsable without writing anything to disk.

**Reading a trajectory.** Every step is a tinted card — one colour per role, so
user, agent and system turns are distinguishable without reading labels — with
its ATIF `step_id` in the gutter to the left of the timeline. The number is also
an anchor, so you can link someone to a specific step. Subagent steps are
numbered independently (ATIF restarts them at 1) and scoped so their anchors
cannot collide with the parent's. Tool-call arguments are syntax-coloured, and
tool output is coloured only when it really parses as JSON, so ordinary command
output is left as plain text. The session list collapses with the button at the
top left, or the `\\` key.

**Branching.** A delegated subagent is a complete trajectory in its own right, so
the viewer renders it as one: collapsed under the tool call that spawned it,
labelled with the agent type, its task, and its step count. Expanding it shows
that agent's own steps — and because ATIF nests arbitrarily deep, a subagent that
delegates further renders the same way, with depth marked. Where a ref points at
an external file (`--split-subagents`) rather than an embedded trajectory, the
viewer says so instead of silently showing nothing.

Because branches sit anywhere in a trajectory that may run to thousands of
steps, every session with branches gets a jump list at the top — agent type,
task, step count — and an "only branches" filter. Steps render 250 at a time so
a 8,000-step session stays responsive.

## Tests

```sh
uv run pytest
```

The suite starts a real server on an ephemeral port and exercises the endpoints,
including that it binds loopback only.
