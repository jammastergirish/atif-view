# atif-view

Browse [ATIF v1.7](https://github.com/harbor-framework/harbor/blob/main/rfcs/0001-trajectory-format.md)
trajectories in a local web viewer.

Conversion lives in [`atif-make`](https://github.com/jammastergirish/atif-make); this package depends on it and
adds only the browser interface.

## Install

```sh
uv tool install atif-view        # pulls atif-make automatically
```

```sh
atif-view                        # everything in the atif-make index
atif-view path/to/session.jsonl  # one log
atif-view bundle.zip             # a bundle someone sent you
atif-view --port 8080 --no-open
```

Build the index first with `atif-make index`, or point the viewer at a file,
directory or archive and it will scan that instead.

## The library

Sessions are organised, not just listed. Rename one, file it into a collection
(nested with `/`), tag it across collections, star it. The collection rail on
the left rolls counts up the tree; the filter bar narrows by tag, or to files
you opened yourself.

Annotations live in `~/.atif/library.json`, keyed by a hash of the file's
content rather than its path — so a name survives the transcript moving, and a
full re-index can never destroy one. What a transcript *is* stays in
`atif-make`'s index; what you decided about it stays here.

Files you open are copied into `~/.atif/opened/`, so they outlast the viewer and
can be organised like anything else. Deleting one removes its copy; deleting a
scanned session only forgets the annotation, never the transcript.

**Reading output.** Tool arguments and any JSON result are syntax-coloured; the
`Raw` tab pretty-prints and colours the source through the same renderer, so a
log reads the same there as it does in a step. Shell output is coloured for the
three things it gets scanned for — what changed, what failed, what passed —
measured against a real corpus rather than tinting every line.

**Provenance is recorded, not applied.** A transcript you opened carries a quiet
tray mark — never a tag pill, because that is a label *you* chose and this is a
fact about where the file came from.

## Asking Claude about a transcript

Two optional AI features, both off until you press something:

- **explain this call** — on any tool call, summarises what it tried to do and
  what came back. The summary is kept, so you pay for it once — and once it
  exists the call simply shows it, with no button to press.
- **Ask Claude** — a collapsible panel holding a conversation about the
  session. Most sessions go to the model whole; only one too large for the
  budget is sampled, and then by scoring each step against the question. Of 81
  sessions here, 58 are sent entire and 23 sampled.
  Follow-ups carry the earlier questions and answers, but not their step dumps:
  those are already digested into the answers, and replaying a page of
  transcript per turn would make a long conversation quadratic. An answer
  reports what it read — "read all 62 steps", or "read 40 of 312 steps" — so a
  partial view is visible rather than implied, and the step numbers it cites are
  links into the transcript.

When sampling is needed, steps are scored by how many of the question's words
they mention, longer words counting for more, with a long word also scoring at a
discount on its first four characters so "authentication" finds `test_auth.py`.
Candidates are trimmed to the budget in score order and then read back
chronologically, so what survives is the most useful rather than the earliest.
A question that matches nothing falls back to the steps the previous answer
read, or, on a first question, to the closing steps.

Both stream: text appears as it is written rather than after the call finishes.
The response is newline-delimited JSON read with `fetch`, not server-sent
events — `EventSource` reconnects when the connection closes, which would
silently repeat a paid call. The framing makes no difference to how finely
tokens arrive; SSE would stream exactly the same. `X-Content-Type-Options:
nosniff` is set, or the browser withholds the opening bytes while it sniffs the
type.

Measured, rather than assumed: over loopback the server delivers 202 of 202
frames individually at a 3.8 ms median gap, with `TCP_NODELAY` making no
difference either way, and building the HTML for a 30,000-character answer costs
under a millisecond. What remains is how coarsely the API itself emits text.

Thinking is reported separately from text. A model that thinks for twenty
seconds before its first word is indistinguishable from a hang, so the panel
says "Thinking…" while that is what is happening — the deliberation itself is
never sent to the page. A call summary skips thinking entirely: two sentences
about one tool call are delayed by it, not improved.

A conversation lives in the page, not the library — switching transcripts
starts a fresh one. Call summaries are cached and do persist.

Nothing is sent on load, on hover, or in the background. Every request is one
click, of yours.

```sh
uv tool install "atif-view[ai]"    # brings in the anthropic SDK
```

The SDK and a credential are two separate requirements, and Settings names
whichever is missing — a key saved with no SDK installed still reports as saved,
rather than looking like the save failed. Running from a checkout, the extra is
not implied:

```sh
uv run --extra ai atif-view
```

Then either export `ANTHROPIC_API_KEY`, or paste a key into **Settings** in the
top bar. A key set in Settings is stored at `~/.atif/config.json`, mode `0600`
inside a `0700` directory; it is sent to the Anthropic API and nowhere else, and
is never read back into the page — the page only ever sees its last four
characters. A key in your keychain or password manager is safer than one in a
file, so prefer the environment variable if you have the choice. Settings shows
which of the two is in use, and **Remove** clears the stored one.

With no key configured, every AI control is hidden and the endpoint refuses.

Each transcript also has its own **With AI support** switch. Turn it off and
that session's controls disappear — useful when a transcript holds something that should not
leave the machine. The server enforces it too, so a switched-off transcript is
refused even if a request is made directly.

## Themes

Three, from Diwan: `paper`, `cool` (both light) and `dark`. The control in the
top bar cycles them, as Diwan's own header does, and the choice is remembered
per browser. Note that "light and dark" is really three modes here — two of
Diwan's palettes are light.

## What it shows

```sh
atif-view view              # everything in the index
atif-view view path/to/log  # a single file
atif-view view --port 8080 --no-open
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

**Opening things.** `Open…` in the sidebar takes a normal file dialog, and
files can be dropped anywhere on the window. Either way the upload goes to the
same `corpus.scan()` the CLI uses, so the button and `atif-view <path>` can
never disagree about what counts as openable — logs, converted trajectories and
archives all work. A client-supplied filename is reduced to a leaf before
anything is written, and uploads live in a temporary directory for the session.

**Duration.** Each trajectory reports how long it actually ran, from the first
step's timestamp to the last, in whatever unit fits — these range from seconds
to `63h` across two and a half days.

**Favourites.** Star individual steps inside a transcript — the star sits in
the gutter beside the step number, visible at rest rather than on hover — and the `Favourited` lens filters to them.
Stars are keyed the same way the step anchors are, so one set inside a subagent
(whose ids restart at 1) cannot land on the wrong step. Rename a transcript from
its own heading by double-clicking it, or from the table; both write the same
record.

**Expand all.** Tool calls and branches open collapsed so a long transcript is
readable; one control opens or closes every one of them. It acts on what is
already on screen rather than repainting, so you keep your place.

**Finding things.** A run of several thousand steps needs more than scrolling.
`Search this run` matches across message text, reasoning, tool names, tool
arguments and observation output — the things a reader can actually see. Filter
lenses (`All / User / Agent / System / Tools / Reasoning / Branches`) carry live
counts for the whole run, so you can tell at a glance that a session is 7,628
tool turns and 451 user messages before filtering to any of them.

**Provenance and sources.** Three tabs: `Trajectory` renders the run;
`Raw` shows the head of the original log, so you can see what was converted
rather than trusting the conversion; `Files` lists everything that travelled
with the session — subagent traces, sidecar manifests, bundled images — each
revealable in the file manager. A details strip records the schema version,
detected source format, model, session id and transcript size.

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

## Developing alongside atif-make

The two packages are developed together. Installing the viewer editable makes
its own code live:

```sh
uv tool install --force --editable .
```

That alone still resolves `atif-make` from git, so edits to the converter would
not show up. To run with **both** live, add it explicitly:

```sh
uv run --with-editable ../atif-make atif-view
```

Use that while changing anything in `atif-make`. Reinstall from the index
(`uv tool install --force atif-view`) when you want to test what users actually
get.

## Tests

```sh
uv run pytest
```

While changing `atif-make` at the same time, run against the local converter or
the tests will resolve the published one:

```sh
uv run --with-editable ../atif-make pytest
```

Add `--extra ai` to either command to exercise the AI paths against a real SDK;
the tests stub the model call, so this never contacts the API.

The suite starts a real server on an ephemeral port and exercises the endpoints,
including that it binds loopback only. It is isolated from your own library,
index, settings and opened-file store — a frozen default argument once let it
write to them, so there is a test for that too.

Most of the viewer's behaviour is browser JavaScript, which pytest cannot
reach — two real breaks shipped that way, a row click that did nothing and a
trajectory pane stuck on "Converting…". Those checks live in
`tests/page.test.js`, load the real page script against a stub DOM, and run from
`tests/test_page.py` as part of the same suite (skipped without node).

The AI tests never call the API. Most stub the model call and check the part
that matters when it is wrong — that nothing is sent unasked, that a summary is
paid for once, that a stored key never reaches a response, and that a transcript
switched off is refused by the server rather than merely hidden.

`tests/test_stream.py` is the exception: it drives the one function that does
touch the SDK, using a fake client but the SDK's real exception classes, so a
rename or re-parenting fails here instead of on someone's first paid call. It
found one already — the SDK moved from `httpx` to `httpx2` at 1.0. CI installs
the extra and fails on any skipped test, since a silently skipped test is worse
than no test.

`tests/test_readme.py` checks this file against the code, pairing each claim
with the marker that makes it true. Prose drifts quietly: a documented behaviour
outlived the code twice here, once because an edit matched nothing and reported
success anyway.
