/* The viewer is one HTML page, so its behaviour lives in browser JavaScript
 * that pytest cannot reach — which is how a broken row click and a stuck
 * "Converting…" both shipped. This loads the real page script against a stub
 * DOM and exercises the parts with logic in them.
 *
 * Run: node tests/page.test.js   (pytest runs it too, via test_page.py)
 */
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");

// ---- the page, extracted from the module that serves it ----------------------
const viewer = readFileSync(
  join(__dirname, "..", "src", "atif_view", "viewer.py"),
  "utf8",
);
const pageStart = viewer.indexOf('PAGE = r"""');
const page = viewer.slice(pageStart, viewer.indexOf('"""', pageStart + 11));
const script = page.slice(
  page.indexOf("<script>") + 8,
  page.lastIndexOf("</script>"),
);

// ---- just enough DOM for the script to load ----------------------------------
const el = () => ({
  innerHTML: "",
  textContent: "",
  value: "",
  style: {},
  classList: { toggle() {}, add() {}, remove() {} },
  setAttribute() {},
  getAttribute: () => "paper",
  focus() {},
  setSelectionRange() {},
  querySelectorAll: () => [],
});
global.document = {
  documentElement: el(),
  body: el(),
  addEventListener() {},
  createElement: el,
  getElementById: () => null,
};
global.addEventListener = () => {};
global.localStorage = { getItem: () => null, setItem() {} };
global.fetch = () =>
  Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
for (const id of [
  "q",
  "list",
  "main",
  "count",
  "toggle",
  "picker",
  "drop",
  "themes",
  "tagbar",
  "theme",
  "crumb",
])
  global[id] = el();

const run = (body) => eval(script + "\n;(() => {" + body + "})()");

const tests = [];
const test = (name, body) => tests.push([name, body]);

// ---- fixtures ----------------------------------------------------------------
const TRAJ = {
  schema_version: "ATIF-v1.7",
  session_id: "s1",
  agent: { name: "claude-code", version: "2.1", model_name: "claude-opus-5" },
  steps: [
    {
      step_id: 1,
      source: "user",
      message: "do the thing",
      timestamp: "2026-01-01T00:00:00Z",
    },
    {
      step_id: 2,
      source: "agent",
      message: "on it",
      timestamp: "2026-01-01T00:05:00Z",
      tool_calls: [
        {
          tool_call_id: "c1",
          function_name: "Bash",
          arguments: { command: "ls" },
        },
      ],
      observation: {
        results: [{ source_call_id: "c1", content: "README.md" }],
      },
    },
    {
      step_id: 3,
      source: "agent",
      message: "done",
      reasoning_content: "thinking",
      timestamp: "2026-01-01T02:00:00Z",
    },
  ],
  subagent_trajectories: [
    {
      trajectory_id: "sub-a",
      agent: { name: "claude-code", version: "2.1" },
      steps: [{ step_id: 2, source: "agent", message: "inner" }],
    },
  ],
};
const ROW = {
  key: "K1",
  origin: "scanned",
  agent: "claude-code",
  format: "claude-code-transcript",
  path: "/tmp/a.jsonl",
  project: "proj",
  session_id: "s1",
  modified: "2026-01-01T00:00:00Z",
  size_bytes: 1048576,
  subagents: 0,
  title: "",
  folder: "",
  tags: [],
  starred: false,
  starred_steps: [],
};

const setup = (row = ROW, traj = TRAJ) => `
  INDEX=[${JSON.stringify(row)}]; FOLDERS=[]; TAGS=[];
  traj=${JSON.stringify(traj)}; cur="${row.key}";
`;

// ---- step keys ---------------------------------------------------------------
test("a subagent step key is scoped, so it cannot collide with the parent's", () => {
  const out = run(
    setup() +
      `
    const parent = step(traj.steps[1], traj, 0, 1, "");
    const inner  = step(traj.subagent_trajectories[0].steps[0], traj.subagent_trajectories[0],
                        1, null, "sub-a-");
    return { parent, inner };
  `,
  );
  // Both are "step 2"; only the scoping keeps them apart.
  assert.match(out.parent, /id="step-2"/);
  assert.match(out.inner, /id="step-sub-a-2"/);
  assert.ok(
    !out.inner.includes('id="step-2"'),
    "subagent step reused the parent's id",
  );
});

test("starring a subagent step does not star the parent's step of the same number", () => {
  const row = { ...ROW, starred_steps: ["sub-a-2"] };
  const out = run(
    setup(row) +
      `
    return { parent: step(traj.steps[1], traj, 0, 1, ""),
             inner:  step(traj.subagent_trajectories[0].steps[0],
                          traj.subagent_trajectories[0], 1, null, "sub-a-") };
  `,
  );
  assert.ok(
    /sstar on/.test(out.inner),
    "the starred subagent step is not marked",
  );
  assert.ok(
    !/sstar on/.test(out.parent),
    "starring a subagent step marked the parent's",
  );
});

test("a top-level star marks only the top-level step", () => {
  const row = { ...ROW, starred_steps: ["2"] };
  const out = run(
    setup(row) +
      `
    return { parent: step(traj.steps[1], traj, 0, 1, ""),
             inner:  step(traj.subagent_trajectories[0].steps[0],
                          traj.subagent_trajectories[0], 1, null, "sub-a-") };
  `,
  );
  assert.ok(/sstar on/.test(out.parent));
  assert.ok(!/sstar on/.test(out.inner));
});

test("the star is rendered on every step, not only on hover", () => {
  // A hover-only affordance is one nobody discovers.
  const out = run(setup() + `return step(traj.steps[0], traj, 0, 0, "");`);
  assert.match(out, /class="sstar"/);
  assert.match(out, /Add to favourites/);
});

// ---- opening a row -----------------------------------------------------------
test("a single click on the title opens the session", () => {
  const opened = run(
    setup() +
      `
    const log=[]; pick=(k)=>log.push(k); showLibrary=()=>{};
    titleClick({stopPropagation(){},target:{closest:()=>null}}, "K1");
    return new Promise(r=>setTimeout(()=>r(log), 350));
  `,
  );
  return opened.then((log) => assert.deepEqual(log, ["K1"]));
});

test("a double click renames instead of opening", () => {
  const result = run(
    setup() +
      `
    const log=[]; pick=(k)=>log.push(k); showLibrary=()=>{};
    const ev={stopPropagation(){},target:{closest:()=>null}};
    titleClick(ev,"K1"); startEdit("K1","title");
    return new Promise(r=>setTimeout(()=>r({log, editing:EDITING}), 350));
  `,
  );
  return result.then(({ log, editing }) => {
    assert.deepEqual(log, [], "a double click also opened the session");
    assert.equal(editing.field, "title");
  });
});

test("clicking a row action does not open the session", () => {
  const log = run(
    setup() +
      `
    const log=[]; pick=(k)=>log.push(k);
    openRow({target:{closest:(s)=>s==="input,button"?{}:null}}, "K1");
    return log;
  `,
  );
  assert.deepEqual(log, []);
});

// ---- the trajectory view -----------------------------------------------------
test("render finds its session by key, not by list position", () => {
  // cur is a content key; indexing INDEX by it once left the pane on "Converting…".
  const painted = run(setup() + `render(); return main.innerHTML;`);
  assert.ok(painted.length > 200, "nothing was painted");
  assert.match(painted, /claude-opus-5/);
});

test("duration reports the span from the first step to the last", () => {
  const shown = run(setup() + `render(); return main.innerHTML;`);
  assert.match(shown, /<b>2h<\/b><span>duration<\/span>/);
});

test("the Favourited lens counts and filters starred steps", () => {
  const row = { ...ROW, starred_steps: ["1", "3"] };
  const out = run(
    setup(row) +
      `
    lens="all"; render();
    const count=(main.innerHTML.match(/Favourited<b>(\\d+)/)||[])[1];
    lens="favourited"; render();
    const shown=(main.innerHTML.match(/class="step /g)||[]).length;
    return {count, shown};
  `,
  );
  assert.equal(out.count, "2");
  assert.equal(out.shown, 2);
});

test("starring a step keeps the reader where they were", async () => {
  // A full repaint used to drop the reader at the top of a long transcript.
  const at = await run(
    setup({ ...ROW, starred_steps: [] }) +
      `
    main.scrollTop = 4200;
    annotate = async () => {};
    return toggleStep({ stopPropagation() {} }, "2").then(() => main.scrollTop);
  `,
  );
  assert.equal(at, 4200);
});

test("expand all opens every tool call and branch when painted", () => {
  const out = run(
    setup() +
      `
    OPEN_ALL = true;
    return { tool: step(traj.steps[1], traj, 0, 1, ""),
             branch: branch({trajectory_id:"sub-a"}, traj, 0) };
  `,
  );
  assert.match(out.tool, /<details class="tool" open>/);
  assert.match(out.branch, /<details class="branch" open>/);
});

test("collapsed is the default, so a long transcript opens readable", () => {
  const out = run(setup() + `return step(traj.steps[1], traj, 0, 1, "");`);
  assert.match(out, /<details class="tool">/);
});

test("toggling expand does not repaint, so the reader keeps their place", () => {
  const out = run(
    setup() +
      `
    const opened=[];
    main.scrollTop = 900;
    main.querySelectorAll=()=>[{set open(v){opened.push(v)}}];
    toggleExpand();
    return {opened, at: main.scrollTop, state: OPEN_ALL};
  `,
  );
  assert.deepEqual(out.opened, [true]);
  assert.equal(out.at, 900);
  assert.equal(out.state, true);
});

// ---- rendering -----------------------------------------------------------------
test("markup in a message is escaped, not rendered", () => {
  const html = run(`return md('<script>alert(1)</script> **bold**');`);
  assert.ok(!html.includes("<script"), "script tag survived");
  assert.match(html, /<strong>bold<\/strong>/);
});

test("shell output colours diffs without reading '---' as a removal", () => {
  const html = run(`return shell("--- a/x.py\\n+++ b/x.py\\n-gone\\n+added");`);
  assert.match(html, /s-meta">--- a\/x\.py/);
  assert.match(html, /s-del">-gone/);
  assert.match(html, /s-add">\+added/);
});

test("a plain listing is left uncoloured", () => {
  assert.ok(
    !/class="s-/.test(run(`return shell("README.md\\nsrc\\npackage.json");`)),
  );
});

test("only http(s) links become anchors", () => {
  const html = run(
    `return md("[x](javascript:alert(1)) and https://example.com");`,
  );
  // The text stays as written; what matters is that nothing links to it.
  const hrefs = [...html.matchAll(/href="([^"]*)"/g)].map((m) => m[1]);
  assert.ok(!hrefs.some((h) => h.startsWith("javascript:")), "a javascript: URL was linked");
  assert.match(html, /href="https:\/\/example\.com"/);
});

test("a path links to the reveal endpoint, in prose and in the files panel", () => {
  const prose = run(`return md("see /Users/me/a.txt");`);
  const files = run(`
    extra={files:[{name:"a & b.jsonl",path:"/Users/me/a.jsonl",role:"source",size:2048}]};
    tab="files"; return panelTab();
  `);
  assert.match(prose, /\/api\/reveal/);
  assert.match(files, /\/api\/reveal/);
  assert.ok(files.includes("a &amp; b.jsonl"), "the file name was not escaped");
});

test("prose that looks like a path is left alone", () => {
  for (const text of ["use and/or as needed", "a 3/4 split", "on 2026/08/20"]) {
    assert.ok(
      !run(`return md(${JSON.stringify(text)});`).includes("/api/reveal"),
      `linked a path in: ${text}`,
    );
  }
});

test("raw JSONL is coloured through the same renderer as tool arguments", () => {
  const out = run(`return rawSource('{"a":1}\\n{"b":"two"}\\n');`);
  assert.match(out.html, /class="j-key"/);
  assert.match(out.html, /class="j-num"/);
  assert.ok(out.html.includes("\n  "), "not pretty-printed");
});

test("raw text that is not JSON is left as text", () => {
  const out = run(`return rawSource("just some notes\\nnot json");`);
  assert.ok(!/class="j-/.test(out.html));
});

test("AI controls stay hidden until a credential is configured", () => {
  assert.strictEqual(run(`AI={};cur="k";INDEX=[{key:"k"}];return aiOn();`), false);
  assert.strictEqual(
    run(`AI={available:true};cur="k";INDEX=[{key:"k"}];return aiOn();`),
    true,
  );
});

test("a transcript switched off hides its AI controls", () => {
  const off = `AI={available:true};cur="k";INDEX=[{key:"k",ai:false}];`;
  assert.strictEqual(run(off + "return aiOn();"), false);
  assert.strictEqual(
    run(`AI={available:true};cur="k";INDEX=[{key:"k",ai:true}];return aiOn();`),
    true,
  );
});

test("the switch itself stays visible when AI is off, so it can be turned back on", () => {
  const html = run(`AI={available:true};cur="k";INDEX=[{key:"k",ai:false}];return aiStrip();`);
  assert.match(html, /type="checkbox"/);
  assert.match(html, /With AI support/);
  assert.ok(!/<input[^>]*\schecked/.test(html), "the box should be unchecked");
  assert.ok(!html.includes("askin"), "the ask box should be hidden");

  const on = run(`AI={available:true};cur="k";INDEX=[{key:"k",ai:true}];return aiStrip();`);
  assert.ok(/<input[^>]*\schecked/.test(on), "the box should be checked when on");
  assert.ok(on.includes("askin"), "the ask box should be shown when on");
});

test("the settings sheet never renders a key, only where one came from", () => {
  const out = run(`
    AI={available:true,source:"settings",hint:"\u20261234",model:"claude-opus-5"};
    const el={textContent:""};
    document.getElementById=()=>el;
    showKeyState();
    return el.textContent;`);
  assert.match(out, /\u20261234/);
  assert.match(out, /AI features are on/);
  assert.ok(!/sk-ant/.test(out));
});

test("a saved key with no SDK reads as saved, not as a failed save", () => {
  const out = run(`
    AI={available:false,source:"settings",hint:"\u2026GQAA",
        reason:"The anthropic package is not installed."};
    const el={textContent:"",className:""};
    document.getElementById=()=>el;
    showKeyState();
    return el.textContent;`);
  assert.match(out, /Key saved here/);
  assert.match(out, /anthropic package is not installed/);
});

test("an unconfigured viewer says so rather than showing a stale key", () => {
  const out = run(`
    AI={};
    const el={textContent:""};
    document.getElementById=()=>el;
    showKeyState();
    return el.textContent;`);
  assert.match(out, /No key saved/);
});

test("an answer renders while it is still arriving", () => {
  const html = run(`
    CHAT=[{q:"why",a:"partly writ",steps:[],busy:true}];
    return turnsHTML();`);
  assert.match(html, /partly writ/);
  assert.match(html, /class="askq">why</, "the question is not shown above its answer");
  assert.ok(!html.includes("askref"), "step count shown before the steps are known");
});

test("thinking is shown as thinking, not as an empty answer", () => {
  const html = run(`
    CHAT=[{q:"why",a:"",steps:[],busy:true,thinking:true}];
    return turnsHTML();`);
  assert.match(html, /Thinking…/);
  assert.match(html, /class="askout busy"/, "no caret while thinking");
});

test("the first token replaces the thinking notice", () => {
  const html = run(`
    CHAT=[{q:"why",a:"because",steps:[],busy:true,thinking:false}];
    return turnsHTML();`);
  assert.ok(!/Thinking…/.test(html));
  assert.match(html, /because/);
});

test("the step count appears once the steps frame lands", () => {
  const html = run(`
    CHAT=[{q:"why",a:"done",steps:[3,9],busy:false}];
    return turnsHTML();`);
  assert.match(html, /from 2 steps/);
});

test("one step reads as a step, not steps", () => {
  const html = run(`CHAT=[{q:"q",a:"a",steps:[4],busy:false}];return turnsHTML();`);
  assert.match(html, /from 1 step</);
});

test("an error replaces that turn's answer rather than appending to it", () => {
  const html = run(`
    CHAT=[{q:"q",a:"",error:"Rate limited",busy:false,steps:[]}];
    return turnsHTML();`);
  assert.match(html, /class="askout bad"/);
  assert.match(html, /Rate limited/);
});

test("nothing renders before a question is asked", () => {
  assert.strictEqual(run(`CHAT=[];return turnsHTML();`), "");
});

test("a conversation keeps every turn, in order", () => {
  const html = run(`
    CHAT=[{q:"first",a:"one",steps:[1],busy:false},
          {q:"second",a:"two",steps:[2],busy:false}];
    return turnsHTML();`);
  assert.ok(html.indexOf("first") < html.indexOf("second"), "turns out of order");
  assert.strictEqual(html.match(/class="turn"/g).length, 2);
});

test("a question is escaped, not rendered as markup", () => {
  const html = run(`
    CHAT=[{q:"<img src=x onerror=alert(1)>",a:"a",steps:[],busy:false}];
    return turnsHTML();`);
  assert.ok(!html.includes("<img"), "a question was rendered as HTML");
  assert.match(html, /&lt;img/);
});

test("the panel offers a follow-up prompt once a conversation exists", () => {
  assert.match(run(`CHAT=[];return askInner();`), /Ask about this transcript/);
  assert.match(
    run(`CHAT=[{q:"a",a:"b",steps:[],busy:false}];return askInner();`),
    /Ask a follow-up/,
  );
});

test("the panel is collapsible and remembers being open", () => {
  const shut = run(`AI={available:true};cur="k";INDEX=[{key:"k"}];ASK_OPEN=false;return aiStrip();`);
  assert.match(shut, /<details class="ask"/);
  assert.ok(!/<details class="ask" open/.test(shut));

  const open = run(`AI={available:true};cur="k";INDEX=[{key:"k"}];ASK_OPEN=true;return aiStrip();`);
  assert.match(open, /<details class="ask" open/);
});

test("switching AI off collapses the panel but keeps its switch reachable", () => {
  const html = run(`
    AI={available:true};cur="k";INDEX=[{key:"k",ai:false}];ASK_OPEN=true;
    return aiStrip();`);
  assert.ok(!/ open/.test(html.split("<summary>")[0]), "panel open with AI off");
  assert.match(html, /type="checkbox"/);
  assert.ok(!html.includes("askin"), "the question field should be gone");
});

test("a streamed response is read frame by frame", async () => {
  const frames = [];
  const body = [
    '{"t":"steps","steps":[1,2]}\n{"t":"delta","text":"he',
    'llo "}\n{"t":"delta","text":"there"}\n',
    '{"t":"done"}',
  ];
  globalThis.__frames = frames;
  await run(`
    globalThis.fetch=async()=>({ok:true,body:{getReader(){
      const parts=${JSON.stringify(body)}.map(s=>new TextEncoder().encode(s));
      let i=0;
      return {read:async()=>i<parts.length?{done:false,value:parts[i++]}:{done:true}};
    }}});
    cur="k";
    return streamClaude({what:"ask"},f=>globalThis.__frames.push(f));`);
  assert.deepStrictEqual(frames.map((f) => f.t), ["steps", "delta", "delta", "done"]);
  assert.strictEqual(frames[1].text + frames[2].text, "hello there");
});

test("an error frame throws rather than rendering as an answer", async () => {
  await assert.rejects(
    run(`
      globalThis.fetch=async()=>({ok:true,body:{getReader(){
        const one=new TextEncoder().encode('{"t":"error","error":"model refused"}');
        let sent=false;
        return {read:async()=>sent?{done:true}:((sent=true),{done:false,value:one})};
      }}});
      cur="k";
      return streamClaude({what:"ask"},()=>{});`),
    /model refused/,
  );
});

test("a refusal before the stream opens surfaces its status message", async () => {
  await assert.rejects(
    run(`
      globalThis.fetch=async()=>({ok:false,status:403,
        json:async()=>({error:"AI is switched off for this transcript."})});
      cur="k";
      return streamClaude({what:"ask"},()=>{});`),
    /switched off/,
  );
});

// ---- runner --------------------------------------------------------------------
(async () => {
  let failed = 0;
  for (const [name, body] of tests) {
    try {
      await body();
      console.log(`  ok   ${name}`);
    } catch (error) {
      failed++;
      console.log(`  FAIL ${name}\n       ${error.message}`);
    }
  }
  console.log(`\n${tests.length - failed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})();
