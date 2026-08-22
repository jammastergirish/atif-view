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
  "modal",
  "urlmodal",
])
  global[id] = el();

/* The page script boots on every eval — it fetches the index and draws. Tests
   stub globals inside their own body, so reset the ones the boot touches first,
   or one test's stub crashes the next test's boot. */
const SAFE_FETCH = () =>
  Promise.resolve({ ok: true, json: () => Promise.resolve({}) });

const run = (body) => {
  global.fetch = SAFE_FETCH;
  return eval(script + "\n;(() => {" + body + "})()");
};

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

test("coverage appears once the steps frame lands", () => {
  const html = run(`
    CHAT=[{q:"why",a:"done",steps:[3,9],total:80,busy:false}];
    return turnsHTML();`);
  assert.match(html, /read 2 of 80 steps/);
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

test("coverage says what was read against the whole transcript", () => {
  const partial = run(`return coverage({steps:new Array(40).fill(0),total:312});`);
  assert.match(partial, /read 40 of 312 steps/);
});

test("coverage says so plainly when the whole transcript was read", () => {
  assert.match(run(`return coverage({steps:[1,2,3],total:3});`), /read all 3 steps/);
  assert.match(run(`return coverage({steps:[1],total:1});`), /read all 1 step</);
});

test("coverage is omitted until the steps are known", () => {
  assert.strictEqual(run(`return coverage({steps:[],total:0});`), "");
});

test("a cited step becomes a link to that step", () => {
  const html = run(`return linkSteps("<p>the parser retried (step 12).</p>");`);
  assert.match(html, /jumpToStep\(12\)/);
  assert.match(html, />12</);
});

test("several cited steps each become their own link", () => {
  const html = run(`return linkSteps("<p>see steps 4, 9 and 11.</p>");`);
  assert.deepStrictEqual(
    [...html.matchAll(/jumpToStep\((\d+)\)/g)].map((m) => m[1]),
    ["4", "9", "11"],
  );
});

test("a number inside code is left alone", () => {
  const html = run(`return linkSteps("<p>run <code>step 7</code> now</p>");`);
  assert.ok(!/jumpToStep/.test(html), "linked a step number inside code");
});

test("a number in a code block is left alone", () => {
  const block = '<pre class="code"><code>step 3</code></pre>';
  const html = run(`return linkSteps(${JSON.stringify(block)});`);
  assert.ok(!/jumpToStep/.test(html));
});

test("prose that is not a citation is left alone", () => {
  for (const text of ["it took 40 attempts", "step by step", "stepped through"]) {
    assert.ok(
      !run(`return linkSteps(${JSON.stringify("<p>" + text + "</p>")});`).includes("jumpToStep"),
      `linked a non-citation in: ${text}`,
    );
  }
});

test("jumping to a step nobody has is ignored rather than throwing", () => {
  assert.doesNotThrow(() =>
    run(`traj={steps:[{step_id:1}]};return jumpToStep(99);`),
  );
});

test("only the streaming turn is rewritten while text arrives", () => {
  const written = [];
  const out = run(`
    CHAT=[{q:"one",a:"first",steps:[1],total:9,busy:false},
          {q:"two",a:"grow",steps:[2],total:9,busy:true}];
    const body={className:"",set innerHTML(v){globalThis.__written.push(v)}};
    const turn={querySelector:()=>body};
    document.querySelector=sel=>sel===".turns"
      ?{querySelectorAll:()=>[{},turn],querySelector:()=>null,
        set innerHTML(v){globalThis.__written.push("WHOLE")}}
      :null;
    return drawTurns();`, (globalThis.__written = written));
  assert.strictEqual(written.length, 1, "more than one node was written");
  assert.ok(!written[0].includes("WHOLE"), "the whole conversation was rebuilt");
  assert.match(written[0], /grow/);
  assert.ok(!written[0].includes("first"), "an earlier answer was re-rendered");
});

test("a call with a cached summary shows it instead of a button", () => {
  const html = run(`
    AI={available:true};cur="k";
    INDEX=[{key:"k",summaries:{c1:"It listed the directory."}}];
    return callSummary("c1");`);
  assert.match(html, /It listed the directory/);
  assert.ok(!html.includes("aibtn"), "still asking for something already paid for");
  assert.ok(!html.includes("hidden"), "the cached summary is hidden");
});

test("a call without one still offers the button", () => {
  const html = run(`
    AI={available:true};cur="k";INDEX=[{key:"k",summaries:{}}];
    return callSummary("c2");`);
  assert.match(html, /explain this call/);
  assert.match(html, /hidden/);
});

test("a blank cached summary is not treated as one", () => {
  const html = run(`
    AI={available:true};cur="k";INDEX=[{key:"k",summaries:{c3:"   "}}];
    return callSummary("c3");`);
  assert.match(html, /explain this call/);
});

test("a session with no annotations at all does not throw", () => {
  assert.doesNotThrow(() =>
    run(`AI={available:true};cur="k";INDEX=[{key:"k"}];return callSummary("c9");`),
  );
});

test("the sheet has a field for every credential the server can hold", () => {
  const html = run(`
    AI={secrets:{
      anthropic:{label:"Anthropic API key",placeholder:"sk-ant-…",env:"ANTHROPIC_API_KEY",source:"",hint:""},
      hf:{label:"Hugging Face token",placeholder:"hf_…",env:"HF_TOKEN",source:"",hint:""},
      github:{label:"GitHub token",placeholder:"ghp_…",env:"GITHUB_TOKEN",source:"",hint:""}}};
    const host={innerHTML:""};
    document.getElementById=id=>id==="secrets"?host:null;
    drawSecrets();
    return host.innerHTML;`);
  for (const label of ["Anthropic API key", "Hugging Face token", "GitHub token"]) {
    assert.ok(html.includes(label), `no field for ${label}`);
  }
  // Three tokens plus the AWS profile, which is a name rather than a secret.
  assert.strictEqual(html.match(/class="secret"/g).length, 4);
  assert.match(html, /AWS profile/);
});

test("a saved token shows as dots with its tail, never as a value", () => {
  const html = run(`
    AI={secrets:{hf:{label:"Hugging Face token",placeholder:"hf_…",
      env:"HF_TOKEN",source:"settings",hint:"\u2026GQAA"}}};
    const host={innerHTML:""};
    document.getElementById=id=>id==="secrets"?host:null;
    drawSecrets();
    return host.innerHTML;`);
  assert.match(html, /placeholder="•+ \u2026GQAA"/);
  const tokenField = html.slice(0, html.indexOf("AWS profile"));
  assert.ok(!/value=/.test(tokenField), "dots as a value could be submitted and stored");
});

test("a token from the environment names the variable rather than showing dots", () => {
  const html = run(`
    AI={secrets:{github:{label:"GitHub token",placeholder:"ghp_…",
      env:"GITHUB_TOKEN",source:"environment",hint:""}}};
    const host={innerHTML:""};
    document.getElementById=id=>id==="secrets"?host:null;
    drawSecrets();
    return host.innerHTML;`);
  assert.match(html, /Set by GITHUB_TOKEN/);
  assert.ok(!html.includes("•"), "dots imply it was saved here and can be removed here");
});

test("an unset token prompts for one", () => {
  const html = run(`
    AI={secrets:{hf:{label:"Hugging Face token",placeholder:"hf_…",
      env:"HF_TOKEN",source:"",hint:""}}};
    const host={innerHTML:""};
    document.getElementById=id=>id==="secrets"?host:null;
    drawSecrets();
    return host.innerHTML;`);
  assert.match(html, /placeholder="hf_…"/);
});

test("a label from the server is escaped, not rendered as markup", () => {
  const html = run(`
    AI={secrets:{x:{label:"<img src=x onerror=alert(1)>",placeholder:"p",
      env:"E",source:"",hint:""}}};
    const host={innerHTML:""};
    document.getElementById=id=>id==="secrets"?host:null;
    drawSecrets();
    return host.innerHTML;`);
  assert.ok(!html.includes("<img"), "a label was rendered as HTML");
});

test("sizes read as KB, MB or GB", () => {
  assert.strictEqual(run(`return bytes(2048);`), "2 KB");
  assert.strictEqual(run(`return bytes(4*1024*1024);`), "4.0 MB");
  assert.strictEqual(run(`return bytes(3*1024*1024*1024);`), "3.0 GB");
  assert.strictEqual(run(`return bytes(10);`), "1 KB", "a tiny file should not read as 0 KB");
});

test("the download reports progress as files land", () => {
  const bar = { hidden: true }, fill = { style: {} }, state = { textContent: "" },
        button = { textContent: "" };
  run(`
    document.getElementById=id=>({urlbar:globalThis.__bar,urlfill:globalThis.__fill,
      urlstate:globalThis.__state,urlgo:globalThis.__button})[id]||null;
    showProgress(3,12,"transcript.jsonl");`,
    (globalThis.__bar = bar), (globalThis.__fill = fill),
    (globalThis.__state = state), (globalThis.__button = button));
  assert.strictEqual(bar.hidden, false, "the bar stayed hidden");
  assert.strictEqual(fill.style.width, "25%");
  assert.match(state.textContent, /3 of 12/);
  assert.match(state.textContent, /transcript\.jsonl/);
});

test("the bar hides again when there is nothing running", () => {
  const bar = { hidden: false }, fill = { style: {} };
  run(`
    document.getElementById=id=>({urlbar:globalThis.__bar,urlfill:globalThis.__fill})[id]||null;
    showProgress(0,0);`,
    (globalThis.__bar = bar), (globalThis.__fill = fill));
  assert.strictEqual(bar.hidden, true);
});

test("a finished download refreshes the index before anything else can fail", async () => {
  const calls = [];
  globalThis.__calls = calls;
  const fields = {
    urlin: { value: "https://github.com/o/r" }, urlinto: { value: "/tmp/x" },
    urlerr: { hidden: true }, urlstate: { textContent: "" },
    urlgo: { textContent: "" }, urlbar: { hidden: true }, urlfill: { style: {} },
  };
  globalThis.__fields = fields;
  await run(`
    document.getElementById=id=>globalThis.__fields[id]||null;
    globalThis.fetch=async(url,opts)=>{
      globalThis.__calls.push(url);
      if(url==="/api/fetch"){
        const body=JSON.parse(opts.body);
        if(!body.confirm)return {ok:true,json:async()=>({plan:true,label:"o--r",
          count:2,bytes:2048,into:"/tmp/x/o--r",names:["a.jsonl"]})};
        const lines=['{"t":"file","done":1,"total":2,"name":"a.jsonl"}',
                     '{"t":"file","done":2,"total":2,"name":"b.jsonl"}',
                     '{"t":"added","added":2,"into":"/tmp/x/o--r"}'];
        const chunk=new TextEncoder().encode(lines.join(String.fromCharCode(10)));
        let sent=false;
        return {ok:true,body:{getReader:()=>({read:async()=>
          sent?{done:true}:((sent=true),{done:false,value:chunk})})}};
      }
      return {ok:true,json:async()=>({sessions:[{key:"a"},{key:"b"}],folders:[],
        tags:[],ai:{},downloads:"/tmp/x"})};
    };
    // Break what the toast depends on, rather than replacing the toast: the
    // point is that the real note() cannot abort the handler.
    document.createElement=()=>{throw new Error("no DOM here")};
    // run() wraps this in a plain arrow, so chain rather than await.
    return planUrl().then(()=>planUrl());`);
  assert.ok(calls.includes("/api/index"), "the index was never refreshed");
  assert.strictEqual(calls.filter((c) => c === "/api/fetch").length, 2);
  assert.strictEqual(fields.urlerr.hidden, true, "a working download showed an error");
});

test("collapsing a node hides everything beneath it, not just its children", () => {
  const rows = run(`
    GROUPS=["Remote","Remote/Hugging Face","Remote/Hugging Face/owner",
            "Remote/Hugging Face/owner/repo","Remote/Hugging Face/owner/repo/attacks",
            "Remote/Hugging Face/owner/repo/attacks/one"];
    INDEX=[{key:"a",group:"Remote/Hugging Face/owner/repo/attacks/one"}];
    // Everything open, then one node in the middle closed.
    EXPANDED={};
    for(const g of GROUPS)EXPANDED[g]=true;
    EXPANDED["Remote/Hugging Face/owner/repo"]=false;
    return visibleRails().map(r=>r.path);`);
  assert.ok(rows.includes("Remote/Hugging Face/owner/repo"), "the closed node itself vanished");
  const beneath = rows.filter((p) => p.startsWith("Remote/Hugging Face/owner/repo/"));
  assert.deepStrictEqual(beneath, [], `still showing: ${beneath.join(", ")}`);
});

test("a node stays visible while every ancestor is open", () => {
  const rows = run(`
    GROUPS=["Local","Local/Claude Code","Local/Claude Code/atif-view"];
    INDEX=[{key:"a",group:"Local/Claude Code/atif-view"}];
    EXPANDED={"Local":true,"Local/Claude Code":true};
    return visibleRails().map(r=>r.path);`);
  assert.ok(rows.includes("Local/Claude Code/atif-view"));
});

test("closing the top node leaves only the top nodes", () => {
  const rows = run(`
    GROUPS=["Local","Local/Claude Code","Local/Claude Code/atif-view","Remote"];
    INDEX=[{key:"a",group:"Local/Claude Code/atif-view"}];
    EXPANDED={"Local":false,"Local/Claude Code":true};
    return visibleRails().map(r=>r.path);`);
  assert.deepStrictEqual(rows, ["__all", "Local", "Remote"]);
});

test("counts still roll up through a closed node", () => {
  const rows = run(`
    GROUPS=["Local","Local/Claude Code","Local/Claude Code/atif-view"];
    INDEX=[{key:"a",group:"Local/Claude Code/atif-view"},
           {key:"b",group:"Local/Claude Code/atif-view"}];
    EXPANDED={};
    return visibleRails();`);
  const local = rows.find((r) => r.path === "Local");
  assert.strictEqual(local.count, 2, "a closed node should still count what is inside");
});

test("the clear control appears only when there is something to clear", () => {
  const empty = run(`
    INDEX=[];GROUPS=[];EXPANDED={};
    const el={innerHTML:""};
    list=el;tagbar={innerHTML:""};
    drawList();
    return el.innerHTML;`);
  assert.ok(!empty.includes("railtop"), "offered to clear an empty library");

  const full = run(`
    INDEX=[{key:"a",group:"Local"}];GROUPS=["Local"];EXPANDED={};
    const el={innerHTML:""};
    list=el;tagbar={innerHTML:""};
    drawList();
    return el.innerHTML;`);
  assert.match(full, /railtop/);
  assert.match(full, /Clear library/);
});

test("clearing asks first, and does nothing when refused", async () => {
  const calls = [];
  globalThis.__cleared = calls;
  await run(`
    INDEX=[{key:"a",origin:"scanned"}];
    globalThis.confirm=()=>false;
    globalThis.fetch=(url,opts)=>{globalThis.__cleared.push(url);
      return Promise.resolve({ok:true,json:async()=>({})})};
    return clearLibrary();`);
  assert.deepStrictEqual(calls, [], "cleared without being confirmed");
});

test("the warning names how many copies the viewer would delete", async () => {
  const asked = [];
  globalThis.__asked = asked;
  await run(`
    INDEX=[{key:"a",origin:"scanned"},{key:"b",origin:"opened"},
           {key:"c",origin:"fetched"}];
    globalThis.confirm=m=>{globalThis.__asked.push(m);return false};
    return clearLibrary();`);
  assert.strictEqual(asked.length, 1);
  assert.match(asked[0], /all 3 sessions/);
  assert.match(asked[0], /1 copy the viewer made itself/);
  assert.match(asked[0], /cached AI summaries/);
});

test("with nothing of ours, the warning says files simply stay", async () => {
  const asked = [];
  globalThis.__asked = asked;
  await run(`
    INDEX=[{key:"a",origin:"scanned"},{key:"c",origin:"fetched"}];
    globalThis.confirm=m=>{globalThis.__asked.push(m);return false};
    return clearLibrary();`);
  assert.match(asked[0], /Files stay where they are\./);
  assert.ok(!asked[0].includes("viewer made"));
});

test("looking on this machine reports what it found", async () => {
  const notes = [];
  globalThis.__notes = notes;
  await run(`
    document.getElementById=()=>null;
    return new Promise(done=>setTimeout(done,0)).then(()=>{
      INDEX=[{key:"a"}];
      cur=null;
      globalThis.__notes.length=0;
      showLibrary=()=>{};
      note=m=>{globalThis.__notes.push(m)};
      globalThis.fetch=async(url)=>({ok:true,json:async()=>url==="/api/scan"
        ?{found:2,total:3}
        :{sessions:[{key:"a"},{key:"b"},{key:"c"}],groups:[],tags:[],ai:{}}});
      return scanMachine();
    });`);
  assert.match(notes[0], /Looking on this machine/);
  assert.match(notes[notes.length - 1], /2 new sessions/);
});

test("looking again when there is nothing new says so", async () => {
  const notes = [];
  globalThis.__notes = notes;
  await run(`
    document.getElementById=()=>null;
    return new Promise(done=>setTimeout(done,0)).then(()=>{
      INDEX=[{key:"a"}];
      cur=null;
      globalThis.__notes.length=0;
      showLibrary=()=>{};
      note=m=>{globalThis.__notes.push(m)};
      globalThis.fetch=async(url)=>({ok:true,json:async()=>url==="/api/scan"
        ?{found:0,total:1}
        :{sessions:[{key:"a"}],groups:[],tags:[],ai:{}}});
      return scanMachine();
    });`);
  assert.match(notes[notes.length - 1], /Nothing new here/);
});

test("Local is offered even when the library is empty", () => {
  const rows = run(`
    INDEX=[];GROUPS=[];EXPANDED={};
    return visibleRails().map(r=>r.path);`);
  assert.deepStrictEqual(rows, ["__all", "Local"]);
});

test("Local is not doubled once it has sessions", () => {
  const rows = run(`
    INDEX=[{key:"a",group:"Local/Claude Code/x"}];
    GROUPS=["Local","Local/Claude Code","Local/Claude Code/x"];
    EXPANDED={};
    return visibleRails().map(r=>r.path);`);
  assert.deepStrictEqual(rows, ["__all", "Local"]);
});

test("Local refreshes by looking again; a remote one by fetching again", () => {
  const html = run(`
    INDEX=[{key:"a",group:"Local"},{key:"b",group:"Remote/S3/bucket",source:"s3://bucket"}];
    GROUPS=["Local","Remote","Remote/S3","Remote/S3/bucket"];
    EXPANDED={"Remote":true,"Remote/S3":true};
    const el={innerHTML:""};
    list=el;tagbar={innerHTML:""};
    drawList();
    return el.innerHTML;`);
  assert.match(html, /scanMachine\(\)/, "Local has no way to look again");
  assert.match(html, /refreshGroup\(&#39;Remote\/S3\/bucket&#39;\)|refreshGroup\('Remote\/S3\/bucket'\)/);
});

test("an s3 source is shown but not linked, having no page to open", () => {
  const html = run(`
    INDEX=[{key:"b",group:"Remote/S3/bucket",source:"s3://bucket/runs"}];
    GROUPS=["Remote","Remote/S3","Remote/S3/bucket"];
    EXPANDED={"Remote":true,"Remote/S3":true};
    const el={innerHTML:""};
    list=el;tagbar={innerHTML:""};
    drawList();
    return el.innerHTML;`);
  assert.ok(!html.includes('href="s3://'), "an s3 URI was made into a link");
});

test("a single file is confirmed, not browsed", async () => {
  const calls = [];
  globalThis.__calls = calls;
  const fields = {
    urlin: { value: "https://example.com/a.jsonl" }, urlerr: { hidden: true },
    urlstate: { textContent: "" }, urlgo: { textContent: "" }, tree: { hidden: true, innerHTML: "" },
  };
  globalThis.__fields = fields;
  await run(`
    document.getElementById=id=>globalThis.__fields[id]||null;
    globalThis.__calls.length=0;
    globalThis.fetch=async(url,opts)=>{
      globalThis.__calls.push(url);
      return {ok:true,json:async()=>({plan:true,label:"example.com",count:1,
        bytes:2048,into:"/tmp/x",names:["a.jsonl"]})};
    };
    return planUrl();`);
  assert.deepStrictEqual(calls, ["/api/fetch"], "a lone file should not be browsed");
  assert.match(fields.urlstate.textContent, /1 file/);
});

test("more than one file opens a picker instead of asking for a number", async () => {
  const calls = [];
  globalThis.__calls = calls;
  const fields = {
    urlin: { value: "s3://bucket" }, urlerr: { hidden: true },
    urlstate: { textContent: "" }, urlgo: { textContent: "" },
    tree: { hidden: true, innerHTML: "" },
  };
  globalThis.__fields = fields;
  await run(`
    document.getElementById=id=>globalThis.__fields[id]||null;
    globalThis.__calls.length=0;
    globalThis.fetch=async(url,opts)=>{
      globalThis.__calls.push(url);
      if(url==="/api/browse")return {ok:true,json:async()=>({nodes:[
        {name:"chippy",path:"chippy",kind:"folder",size:null},
        {name:"one.jsonl",path:"one.jsonl",kind:"file",size:4096}]})};
      return {ok:true,json:async()=>({plan:true,label:"bucket",count:9,bytes:99,
        into:"/tmp/x",names:[]})};
    };
    return planUrl();`);
  assert.deepStrictEqual(calls, ["/api/fetch", "/api/browse"]);
  assert.strictEqual(fields.tree.hidden, false, "the picker stayed hidden");
  assert.match(fields.tree.innerHTML, /chippy/);
  assert.match(fields.tree.innerHTML, /one\.jsonl/);
});

test("ticking a folder covers what is inside it", () => {
  const out = run(`
    TREE={url:"s3://b",open:{},nodes:{},picked:new Set(),busy:new Set()};
    pickNode("chippy");
    return [isPicked("chippy"),isPicked("chippy/abc/x.zip"),isPicked("other")];`);
  assert.deepStrictEqual(out, [true, true, false]);
});

test("ticking a folder drops the redundant ticks inside it", () => {
  const out = run(`
    TREE={url:"s3://b",open:{},nodes:{},picked:new Set(),busy:new Set()};
    pickNode("chippy/abc");
    pickNode("chippy");
    return [...TREE.picked];`);
  assert.deepStrictEqual(out, ["chippy"], "a nested tick outlived its parent");
});

test("the total counts what has been listed", () => {
  const out = run(`
    TREE={url:"s3://b",open:{},nodes:{
      "":[{name:"a",path:"a",kind:"folder",size:null}],
      "a":[{name:"x.zip",path:"a/x.zip",kind:"file",size:1024},
           {name:"y.zip",path:"a/y.zip",kind:"file",size:2048}]},
      picked:new Set(["a"]),busy:new Set()};
    return picked();`);
  assert.deepStrictEqual(out, { files: 2, bytes: 3072, unknown: false });
});

test("an unopened folder is counted as more, not as nothing", () => {
  const out = run(`
    TREE={url:"s3://b",open:{},nodes:{
      "":[{name:"a",path:"a",kind:"folder",size:null}]},
      picked:new Set(["a"]),busy:new Set()};
    return picked();`);
  assert.strictEqual(out.unknown, true, "a folder nobody opened was counted as empty");
  assert.strictEqual(out.files, 0);
});

test("a ticked file is counted from its parent listing", () => {
  const out = run(`
    TREE={url:"s3://b",open:{},nodes:{
      "a":[{name:"x.zip",path:"a/x.zip",kind:"file",size:5000}]},
      picked:new Set(["a/x.zip"]),busy:new Set()};
    return picked();`);
  assert.deepStrictEqual(out, { files: 1, bytes: 5000, unknown: false });
});

test("the profile is a chooser only when there is a choice", () => {
  const one = run(`AI={aws_profiles:["rw-eng"],aws_profile:""};return awsField();`);
  assert.ok(!one.includes("<select"), "asked to choose between one thing");
  assert.match(one, /nothing to set here/);

  const many = run(`AI={aws_profiles:["a","b"],aws_profile:"b"};return awsField();`);
  assert.match(many, /<select/);
  assert.match(many, /value="b" selected/);
  assert.match(many, /pick the one to use/);
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
