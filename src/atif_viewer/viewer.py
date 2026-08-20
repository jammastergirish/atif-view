"""Local trajectory viewer.

Serves a single self-contained page on loopback. Trajectories are converted on
demand and cached in memory, so opening the viewer over a large corpus is cheap
and only the sessions actually opened pay conversion cost.

The server binds 127.0.0.1 only — it reads local session logs, which routinely
contain source code and credentials in tool output, and must never be reachable
off-host.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import webbrowser
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from atif_make.atif import ContentPart, Trajectory
from atif_make.convert import convert
from atif_make.corpus import Entry, scan

PAGE = r"""<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>atif-viewer</title>
<style>
:root{
  --bg:#faf9f7; --panel:#fff; --sunk:#f3f1ed; --ink:#191817; --dim:#6f6a63; --faint:#9a948c;
  --line:#e6e1d9; --user:#1d4ed8; --agent:#6d28d9; --system:#0f766e; --tool:#b45309;
  --accent:#0f766e; --shadow:0 1px 2px rgba(0,0,0,.05);
  --user-bg:#eef3fd; --agent-bg:#f5f1fd; --system-bg:#ecf6f4;
  --j-key:#0550ae; --j-str:#0a7d3f; --j-num:#b45309; --j-lit:#8b2fc9;
}
@media(prefers-color-scheme:dark){:root{
  --bg:#141417; --panel:#1c1c20; --sunk:#232329; --ink:#eceae6; --dim:#a09a92; --faint:#6f6a63;
  --line:#2e2e35; --user:#7ea6ff; --agent:#c4b5fd; --system:#5eead4; --tool:#fbbf24;
  --accent:#5eead4; --shadow:none;
  --user-bg:#182231; --agent-bg:#221d33; --system-bg:#15272a;
  --j-key:#8fb6ff; --j-str:#6ee7a8; --j-num:#fbbf24; --j-lit:#d3bcff;
}}
*{box-sizing:border-box;margin:0}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline;text-underline-offset:2px}
a.gone{color:var(--faint);text-decoration:line-through}
body{font:14.5px/1.6 ui-sans-serif,-apple-system,"Segoe UI",sans-serif;background:var(--bg);color:var(--ink);display:flex;height:100vh;overflow:hidden}
kbd,code,pre{font-family:ui-monospace,"SF Mono",Menlo,monospace}

/* ---- sidebar ---- */
#side{width:320px;flex:0 0 320px;border-right:1px solid var(--line);background:var(--panel);display:flex;flex-direction:column}
#brand{padding:16px 16px 10px;font-weight:700;letter-spacing:-.01em;display:flex;align-items:baseline;gap:8px}
#brand small{font-weight:400;color:var(--faint);font-size:11.5px}
#q{margin:0 16px 10px;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);font-size:13px;width:calc(100% - 32px)}
#q:focus{outline:2px solid var(--accent);outline-offset:-1px}
#list{overflow:auto;flex:1;padding-bottom:20px}
.group{padding:14px 16px 5px;font-size:10.5px;letter-spacing:.13em;text-transform:uppercase;color:var(--faint);font-weight:600}
.item{padding:9px 16px;cursor:pointer;border-left:3px solid transparent}
.item:hover{background:var(--sunk)}
.item.on{background:var(--sunk);border-left-color:var(--accent)}
.item .t{font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.item .m{margin-top:3px;color:var(--dim);font-size:11px;display:flex;gap:7px;align-items:center;flex-wrap:wrap}
.pill{border:1px solid var(--line);border-radius:999px;padding:1px 7px;font-size:10px;color:var(--dim);white-space:nowrap}
.pill.br{border-color:var(--accent);color:var(--accent)}

/* ---- main ---- */
#main{flex:1;overflow:auto;padding:28px 34px 90px;max-width:1100px}

/* Sidebar toggle. The button is fixed rather than inside #main, which is
   replaced wholesale on every render. */
#toggle{position:fixed;top:10px;left:280px;z-index:20;width:28px;height:28px;padding:0;
  border:1px solid var(--line);border-radius:7px;background:var(--panel);color:var(--dim);
  font:inherit;font-size:15px;line-height:1;cursor:pointer;transition:left .15s ease}
#toggle:hover{color:var(--ink);background:var(--sunk)}
body.hide-side #side{display:none}
body.hide-side #toggle{left:10px}
/* Keep the heading clear of the button once the sidebar is gone. */
body.hide-side #main{padding-left:52px}
.hd h2{font-size:21px;letter-spacing:-.02em;overflow-wrap:anywhere}
.hd .sub{color:var(--dim);font-size:12.5px;margin-top:4px;overflow-wrap:anywhere}
.stats{display:flex;gap:26px;flex-wrap:wrap;padding:16px 0 18px;margin:16px 0 22px;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.stat b{display:block;font-size:18px;font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.stat span{color:var(--faint);font-size:10.5px;text-transform:uppercase;letter-spacing:.09em}
.filters{display:flex;gap:6px;margin-bottom:18px;flex-wrap:wrap}
.chip{border:1px solid var(--line);background:var(--panel);color:var(--dim);border-radius:999px;padding:3px 11px;font-size:12px;cursor:pointer}
.chip.on{background:var(--ink);color:var(--bg);border-color:var(--ink)}
/* Divider between the role filters and the display toggles. */
.fsep{width:1px;background:var(--line);align-self:stretch;margin:1px 6px}

/* ---- steps ---- */
.step{position:relative;margin:0 0 10px 46px;padding:9px 15px 11px 15px;
  border-left:2px solid var(--line);border-radius:0 9px 9px 0;background:var(--panel)}
.step.user{background:var(--user-bg);border-left-color:var(--user)}
.step.agent{background:var(--agent-bg);border-left-color:var(--agent)}
.step.system{background:var(--system-bg);border-left-color:var(--system)}
.step:before{content:"";position:absolute;left:-6px;top:14px;width:9px;height:9px;border-radius:50%;background:var(--line);border:2px solid var(--bg)}
.step.user:before{background:var(--user)}.step.agent:before{background:var(--agent)}.step.system:before{background:var(--system)}
.role{display:flex;gap:10px;align-items:baseline;font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--faint);margin-bottom:6px}
.role b{font-weight:700}
/* The step number sits in the gutter, left of the timeline dot. */
.sid{position:absolute;left:-52px;top:10px;width:38px;text-align:right;
  font-variant-numeric:tabular-nums;font-size:11.5px;font-weight:600;letter-spacing:0;
  color:var(--faint);text-decoration:none}
.sid:hover{color:var(--accent)}
/* Nested subagent steps sit in a narrower gutter of their own. */
.branch .inner .step{margin-left:34px}
.branch .inner .sid{left:-40px;top:10px;width:28px;font-size:11px}
.bnav-i .sid{position:static;width:auto;text-align:left;font-size:11px;color:var(--dim)}
.step.user .role b{color:var(--user)}.step.agent .role b{color:var(--agent)}.step.system .role b{color:var(--system)}
.msg{white-space:pre-wrap;overflow-wrap:anywhere}
.think{margin:8px 0;padding:8px 12px;border-left:2px solid var(--line);background:var(--bg);border-radius:0 6px 6px 0;color:var(--dim);font-size:13.5px;white-space:pre-wrap;overflow-wrap:anywhere}
details.tool{margin-top:9px;border:1px solid var(--line);border-radius:9px;background:var(--panel);box-shadow:var(--shadow);overflow:hidden}
details.tool>summary{cursor:pointer;padding:8px 12px;font-size:12.5px;display:flex;gap:9px;align-items:center;list-style:none}
details.tool>summary::-webkit-details-marker{display:none}
details.tool>summary:before{content:"▸";color:var(--faint);font-size:10px}
details.tool[open]>summary:before{content:"▾"}
.tname{font-family:ui-monospace,monospace;font-weight:600;color:var(--tool)}
.tbody{padding:0 12px 12px}
pre{background:var(--sunk);border-radius:7px;padding:10px;overflow:auto;font-size:12px;max-height:320px;margin-top:8px;white-space:pre-wrap;overflow-wrap:anywhere}

/* ---- branching ---- */
.branch{margin-top:10px;border:1px solid var(--accent);border-radius:9px;background:var(--panel);overflow:hidden}
.branch>summary{cursor:pointer;padding:9px 12px;font-size:12.5px;display:flex;gap:9px;align-items:center;list-style:none;color:var(--accent)}
.branch>summary::-webkit-details-marker{display:none}
.branch>summary:before{content:"⤷";font-weight:700}
.branch>summary b{font-weight:700}
.branch>summary .desc{color:var(--dim);font-weight:400;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.branch .inner{padding:6px 14px 4px 22px;border-top:1px solid var(--line);background:var(--sunk)}
.branch .inner .step{border-left-color:var(--line)}
.miss{padding:9px 12px;color:var(--dim);font-size:12.5px}
.empty{color:var(--faint);padding:60px 0;text-align:center}

/* branch navigator: branches are scattered through thousands of steps */
.bnav{border:1px solid var(--line);border-radius:10px;background:var(--panel);margin-bottom:18px;overflow:hidden}
.bnav-h{padding:9px 13px;font-size:10.5px;letter-spacing:.13em;text-transform:uppercase;color:var(--faint);font-weight:600;border-bottom:1px solid var(--line)}
.bnav-l{max-height:190px;overflow:auto}
.bnav-i{display:grid;grid-template-columns:minmax(70px,auto) 1fr auto;gap:11px;align-items:baseline;width:100%;
  text-align:left;background:none;border:0;border-bottom:1px solid var(--line);padding:7px 13px;cursor:pointer;
  color:var(--ink);font:inherit;font-size:12.5px}
.bnav-i:last-child{border-bottom:0}
.bnav-i:hover{background:var(--sunk)}
.bnav-i b{color:var(--accent);font-weight:600}
.bnav-i span{color:var(--dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bnav-i em{color:var(--faint);font-style:normal;font-size:11px;white-space:nowrap}
/* rendered markdown */
.md{overflow-wrap:anywhere}
.md>*:first-child{margin-top:0}
.md>*:last-child{margin-bottom:0}
.md p{margin:.55em 0}
.md h1,.md h2,.md h3,.md h4{margin:1em 0 .4em;line-height:1.3;letter-spacing:-.01em}
.md h1{font-size:1.32em}.md h2{font-size:1.18em}.md h3{font-size:1.06em}.md h4{font-size:1em;color:var(--dim)}
.md ul,.md ol{margin:.5em 0;padding-left:1.4em}
.md li{margin:.2em 0}
.md code{background:var(--bg);border-radius:4px;padding:.1em .35em;font-size:.88em}
.md pre.code{background:var(--bg);border-radius:7px;padding:10px 12px;overflow:auto;margin:.6em 0;max-height:340px}
.md pre.code code{background:none;padding:0;font-size:12px;line-height:1.5}
.md blockquote{margin:.6em 0;padding:.1em 0 .1em .9em;border-left:2px solid var(--line);color:var(--dim)}
.md table{border-collapse:collapse;margin:.7em 0;font-size:.93em;display:block;overflow-x:auto}
.md th,.md td{border:1px solid var(--line);padding:5px 10px;text-align:left}
.md th{background:var(--sunk);font-weight:600}
.md hr{border:0;border-top:1px solid var(--line);margin:1em 0}
/* JSON syntax colours */
.j-key{color:var(--j-key)}
.j-str{color:var(--j-str)}
.j-num{color:var(--j-num)}
.j-lit{color:var(--j-lit)}
pre.json{line-height:1.45}
.img{display:inline-block;margin:8px 8px 0 0}
.img img{max-width:min(420px,100%);max-height:300px;border:1px solid var(--line);border-radius:8px;display:block;background:var(--sunk)}
.more{padding:20px 0;text-align:center}
.more button{border:1px solid var(--line);background:var(--panel);color:var(--dim);border-radius:999px;
  padding:8px 18px;font:inherit;font-size:12.5px;cursor:pointer}
.more button:hover{background:var(--sunk);color:var(--ink)}
</style>

<div id="side">
  <div id="brand">atif <small id="count"></small></div>
  <input id="q" placeholder="filter sessions…">
  <div id="list"></div>
</div>
<button id="toggle" onclick="toggleSide()" title="Show/hide sessions (\\)" aria-label="Toggle sidebar">&lsaquo;</button>
<div id="main"><div class="empty">Select a session.</div></div>

<script>
// Minimal Markdown renderer. Escapes first, then transforms, so no raw HTML
// from a log can ever reach the DOM.
// Bare URLs become links, in prose, JSON strings and tool output alike.
// Splitting on existing anchors, code spans and tags means this pass only ever
// touches plain text, never markup a caller already produced.
// A leading component that plausibly starts a real filesystem path. Requiring
// one of these keeps prose like "and/or" from being read as a path.
const PATH_ROOT = "~|/(?:Users|Volumes|home|opt|etc|var|tmp|private|Applications|srv|mnt)";
// URLs and absolute paths are found in one pass so both get identical
// treatment; the callback decides which kind it matched.
const AUTOLINK = new RegExp(
  "(\\bhttps?://[^\\s<>()\\[\\]\"']+)" +
    "|((?:" + PATH_ROOT + ")(?:/[^\\s<>\"'`]+)*)",
  "g",
);
// Inside a JSON string the quotes delimit the value exactly, so a path there
// may contain spaces — unlike one found loose in prose.
const PATH_EXACT = new RegExp("^(?:" + PATH_ROOT + ")(?:/[^/].*)*$");

const anchor = (href, label) =>
  `<a href="${href}" target="_blank" rel="noreferrer noopener">${label}</a>`;
// Revealing happens server-side: Chrome refuses to follow file:// from an http
// page, so the link calls back to the local server instead.
const pathAnchor = (path) =>
  `<a href="/api/reveal?path=${encodeURIComponent(path)}" onclick="return reveal(this)"` +
  ` title="Reveal in Finder">${path}</a>`;
const autolink = (html) =>
  html
    .split(/(<a\b[^>]*>[\s\S]*?<\/a>|<[^>]+>)/g)
    .map((part, i) =>
      i % 2
        ? part
        : part.replace(AUTOLINK, (m, url, path) => {
            // Trailing punctuation belongs to the sentence, not the target.
            let value = url || path,
              rest = "";
            const trail = value.match(/(?:&(?:lt|gt|quot|amp);|[.,;:!?)\]}'"])+$/);
            if (trail) { value = value.slice(0, -trail[0].length); rest = trail[0]; }
            if (!value) return m;
            return (url ? anchor(value, value) : pathAnchor(value)) + rest;
          }),
    )
    .join("");

function md(src) {
  if (!src) return "";
  let s = String(src)
    .replace(/\r\n?/g, "\n")
    .replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c]);

  // Fenced code is pulled out first so its contents are never marked up.
  const blocks = [];
  s = s.replace(/```([\w+-]*)\n([\s\S]*?)```/g, (m, lang, code) => {
    blocks.push(
      `<pre class="code"${lang ? ` data-lang="${lang}"` : ""}><code>${code.replace(/\n$/, "")}</code></pre>`,
    );
    return `\n<!--B${blocks.length - 1}-->\n`;
  });

  const inline = (t) =>
    t
      .replace(/`([^`\n]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s.,;:)!?]|$)/g, "$1<em>$2</em>")
      .replace(/(^|[\s(])_([^_\n]+)_(?=[\s.,;:)!?]|$)/g, "$1<em>$2</em>")
      .replace(/~~([^~\n]+)~~/g, "<del>$1</del>")
      // Only http(s) links become anchors; anything else stays as plain text.
      .replace(
        /\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g,
        '<a href="$2" target="_blank" rel="noreferrer noopener">$1</a>',
      );

  const lines = s.split("\n"),
    out = [];
  let para = [],
    list = null,
    quote = [],
    table = null;

  const flushPara = () => {
    if (para.length) {
      out.push(`<p>${inline(para.join(" "))}</p>`);
      para = [];
    }
  };
  const flushList = () => {
    if (list) {
      out.push(
        `<${list.tag}>${list.items.map((i) => `<li>${inline(i)}</li>`).join("")}</${list.tag}>`,
      );
      list = null;
    }
  };
  const flushQuote = () => {
    if (quote.length) {
      out.push(`<blockquote>${inline(quote.join(" "))}</blockquote>`);
      quote = [];
    }
  };
  const flushTable = () => {
    if (!table) return;
    const cells = (r) =>
      r
        .split("|")
        .slice(1, -1)
        .map((c) => c.trim());
    const head = cells(table[0])
      .map((c) => `<th>${inline(c)}</th>`)
      .join("");
    const body = table
      .slice(2)
      .map(
        (r) =>
          `<tr>${cells(r)
            .map((c) => `<td>${inline(c)}</td>`)
            .join("")}</tr>`,
      )
      .join("");
    out.push(
      `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`,
    );
    table = null;
  };
  const flushAll = () => {
    flushPara();
    flushList();
    flushQuote();
    flushTable();
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i],
      t = line.trim();

    if (/^<!--B\d+-->$/.test(t)) {
      flushAll();
      out.push(t);
      continue;
    }
    if (!t) {
      flushAll();
      continue;
    }

    // A pipe table needs its delimiter row to be a table at all.
    if (
      !table &&
      /^\|.*\|$/.test(t) &&
      /^\|[\s:|-]+\|$/.test((lines[i + 1] || "").trim())
    ) {
      flushPara();
      flushList();
      flushQuote();
      table = [t, (lines[i + 1] || "").trim()];
      i++;
      continue;
    }
    if (table) {
      if (/^\|.*\|$/.test(t)) {
        table.push(t);
        continue;
      }
      flushTable();
    }

    let m;
    if ((m = t.match(/^(#{1,6})\s+(.*)$/))) {
      flushAll();
      out.push(`<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`);
      continue;
    }
    if (/^([-*_])\1{2,}$/.test(t)) {
      flushAll();
      out.push("<hr>");
      continue;
    }
    if ((m = t.match(/^&gt;\s?(.*)$/))) {
      flushPara();
      flushList();
      flushTable();
      quote.push(m[1]);
      continue;
    }
    flushQuote();

    if ((m = t.match(/^[-*+]\s+(.*)$/))) {
      flushPara();
      flushTable();
      if (!list || list.tag !== "ul") {
        flushList();
        list = { tag: "ul", items: [] };
      }
      list.items.push(m[1]);
      continue;
    }
    if ((m = t.match(/^\d+[.)]\s+(.*)$/))) {
      flushPara();
      flushTable();
      if (!list || list.tag !== "ol") {
        flushList();
        list = { tag: "ol", items: [] };
      }
      list.items.push(m[1]);
      continue;
    }
    // A continuation line belongs to the open list item, not a new paragraph.
    if (list && /^\s{2,}\S/.test(line)) {
      list.items[list.items.length - 1] += " " + t;
      continue;
    }
    flushList();
    para.push(t);
  }
  flushAll();

  return autolink(
    out.join("").replace(/<!--B(\d+)-->/g, (m, n) => blocks[+n]),
  );
}

// Escape only the three characters that matter in element text. Quotes are
// left intact so the tokenizer below can still see JSON string delimiters.
const escText = (s) =>
  String(s ?? "").replace(
    /[&<>]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c],
  );

// Colourise a JSON value. Runs over already-escaped text, so nothing it emits
// can introduce markup.
function hjson(value, indent = 2) {
  let text;
  try {
    text = JSON.stringify(value, null, indent);
  } catch (e) {
    return escText(String(value));
  }
  if (text === undefined) return "";
  const coloured = escText(text).replace(
    /("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g,
    (m, str, colon, lit) => {
      if (str !== undefined) {
        if (colon) return `<span class="j-key">${str}</span>${colon}`;
        const inner = str.slice(1, -1);
        // Quotes bound the value, so a path with spaces is unambiguous here.
        if (PATH_EXACT.test(inner)) {
          return `<span class="j-str">"${pathAnchor(inner)}"</span>`;
        }
        return `<span class="j-str">${str}</span>`;
      }
      if (lit !== undefined) return `<span class="j-lit">${m}</span>`;
      return `<span class="j-num">${m}</span>`;
    },
  );
  // A URL inside a JSON string value should be clickable too.
  return autolink(coloured);
}

// Tool output is usually plain text, sometimes JSON. Colour it only when it
// really parses, so a log line that merely starts with "{" is left alone.
function pre(content) {
  const t = String(content ?? "").trim();
  if (
    (t.startsWith("{") && t.endsWith("}")) ||
    (t.startsWith("[") && t.endsWith("]"))
  ) {
    try {
      return `<pre class="json">${hjson(JSON.parse(t))}</pre>`;
    } catch (e) {
      /* not JSON after all */
    }
  }
  return `<pre>${autolink(escText(content))}</pre>`;
}

const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const num=n=>(n??0).toLocaleString();
const short=s=>{const p=String(s||"").split("/").filter(Boolean);return p.slice(-2).join("/")||s};
const PAGE_SIZE=250;
let INDEX=[],cur=null,traj=null,show={user:1,agent:1,system:1},onlyBranches=false,limit=PAGE_SIZE,raw=false;

function reveal(a){
  fetch(a.getAttribute("href")).then(r=>{
    // A path recorded in an old log may simply not exist any more.
    if(!r.ok)a.classList.add("gone");
  });
  return false;
}

function toggleSide(){
  const hidden=document.body.classList.toggle("hide-side");
  toggle.innerHTML=hidden?"&rsaquo;":"&lsaquo;";
  try{localStorage.setItem("atif-viewer.side",hidden?"1":"0")}catch(e){}
}
// Restore the last choice; localStorage can throw in restricted contexts.
try{if(localStorage.getItem("atif-viewer.side")==="1")toggleSide()}catch(e){}

addEventListener("keydown",e=>{
  // Ignore the shortcut while typing in the filter box.
  if(e.key==="\\"&&e.target.tagName!=="INPUT"){e.preventDefault();toggleSide()}
});

fetch("/api/index").then(r=>r.json()).then(d=>{
  INDEX=d;count.textContent=d.length+" sessions";drawList();
});
q.oninput=drawList;

function drawList(){
  const f=q.value.toLowerCase();
  const rows=INDEX.map((e,i)=>({e,i})).filter(({e})=>
    !f||((e.project||"")+" "+(e.session_id||"")+" "+e.agent+" "+e.format).toLowerCase().includes(f));
  const groups={};
  for(const r of rows)(groups[r.e.agent]??=[]).push(r);
  list.innerHTML=Object.entries(groups).map(([agent,rs])=>
    `<div class="group">${esc(agent)} · ${rs.length}</div>`+rs.map(({e,i})=>`
      <div class="item${cur===i?" on":""}" onclick="pick(${i})">
        <div class="t">${esc(short(e.project||e.session_id||e.path))}</div>
        <div class="m">
          <span>${e.modified?e.modified.slice(0,10):"—"}</span>
          <span>${(e.size_bytes/1048576).toFixed(1)} MB</span>
          ${e.subagents?`<span class="pill br">${e.subagents} branches</span>`:""}
          ${e.format==="atif"?`<span class="pill">imported</span>`:""}
        </div></div>`).join("")).join("")
    ||`<div class="empty" style="padding:30px 16px">No matches.</div>`;
}

function pick(i){
  cur=i;drawList();
  main.innerHTML=`<div class="empty">Converting…</div>`;
  fetch("/api/trajectory?i="+i).then(r=>r.json()).then(t=>{
    if(t.error){main.innerHTML=`<div class="empty">${esc(t.error)}</div>`;return}
    traj=t;onlyBranches=false;limit=PAGE_SIZE;render();
  });
}

const toggle=r=>{show[r]=!show[r];limit=PAGE_SIZE;render()};
const toggleBranches=()=>{onlyBranches=!onlyBranches;limit=PAGE_SIZE;render()};
const more=()=>{limit+=PAGE_SIZE*4;render()};
const toggleRaw=()=>{raw=!raw;render()};

/* Every step that delegates, with the trajectory it delegates to. */
function branchIndex(t){
  const out=[];
  t.steps.forEach((s,i)=>{
    for(const c of s.tool_calls||[]){
      const r=(s.observation?.results||[]).find(x=>x.source_call_id===c.tool_call_id);
      for(const ref of r?.subagent_trajectory_ref||[]){
        const sub=(t.subagent_trajectories||[]).find(x=>x.trajectory_id===ref.trajectory_id);
        out.push({stepIndex:i,stepId:s.step_id,ref,sub});
      }
    }
  });
  return out;
}

/* `pos` is the array position (what windowing counts); `id` is the ATIF
   step_id (what the anchor and the label use). They differ by one, so keep
   them distinct rather than letting the label drift from the link. */
function jump(pos,id){
  // The target may be outside the current window, or hidden by a filter.
  if(pos>=limit){limit=pos+PAGE_SIZE;render()}
  const el=document.getElementById("step-"+id);
  if(!el)return;
  el.scrollIntoView({behavior:"smooth",block:"center"});
  el.querySelectorAll("details.branch").forEach(d=>d.open=true);
  el.style.transition="none";el.style.background="var(--sunk)";
  setTimeout(()=>{el.style.transition="background .8s";el.style.background=""},600);
}

function render(){
  const t=traj,e=INDEX[cur],m=t.final_metrics||{},a=t.agent||{};
  const branches=branchIndex(t);
  const bySteps=new Set(branches.map(b=>b.stepIndex));

  let rows=t.steps.map((s,i)=>({s,i})).filter(({s,i})=>show[s.source]&&(!onlyBranches||bySteps.has(i)));
  const total=rows.length;
  const shown=rows.slice(0,limit);

  main.innerHTML=`
    <div class="hd">
      <h2>${esc(short(e.project||t.session_id||"trajectory"))}</h2>
      <div class="sub">${esc(a.name)} ${esc(a.version||"")} · ${esc(a.model_name||"no model")} · ${esc(t.session_id||"")}</div>
    </div>
    <div class="stats">
      ${stat(t.steps.length,"steps")}
      ${stat(t.steps.filter(s=>s.tool_calls).length,"tool turns")}
      ${branches.length?stat(branches.length,"branches"):""}
      ${stat(m.total_prompt_tokens,"prompt")}
      ${stat(m.total_completion_tokens,"output")}
      ${stat(m.total_cached_tokens,"cached")}
    </div>
    ${branches.length?branchNav(branches):""}
    <div class="filters">
      ${["user","agent","system"].map(r=>
        `<span class="chip${show[r]?" on":""}" onclick="toggle('${r}')">${r}</span>`).join("")}
      ${branches.length?`<span class="chip${onlyBranches?" on":""}" onclick="toggleBranches()">only branches</span>`:""}
      <span class="fsep"></span>
      <span class="chip${raw?" on":""}" onclick="toggleRaw()">raw text</span>
    </div>
    ${shown.map(({s,i})=>step(s,t,0,i)).join("")||`<div class="empty">Nothing matches those filters.</div>`}
    ${total>shown.length?`<div class="more"><button onclick="more()">Show more —
        ${num(shown.length)} of ${num(total)} steps</button></div>`:""}`;
}
const stat=(v,l)=>v==null?"":`<div class="stat"><b>${num(v)}</b><span>${l}</span></div>`;

/* A jump list, because branches are scattered through thousands of steps. */
function branchNav(branches){
  return `<div class="bnav"><div class="bnav-h">Branches</div><div class="bnav-l">`+
    branches.map((b,n)=>{
      const x=b.sub?.extra||{};
      const label=x.agentType||b.sub?.agent?.name||"subagent";
      return `<button class="bnav-i" onclick="jump(${b.stepIndex},${b.stepId})" title="${esc(x.description||"")}">
        <span class="sid">${b.stepId}</span>
        <b>${esc(label)}</b>
        <span>${esc(x.description||"")||"—"}</span>
        <em>${b.sub?b.sub.steps.length+" steps":"external"}</em>
      </button>`;
    }).join("")+`</div></div>`;
}

/* A message is either a plain string or ATIF ContentParts (text and images). */
function text(v){
  // Agent messages are written in Markdown; rendering them as such is the
  // difference between a wall of asterisks and something readable.
  return raw?`<div class="msg">${esc(v)}</div>`:`<div class="md">${md(v)}</div>`;
}

function body(v){
  if(typeof v==="string")return text(v);
  if(!Array.isArray(v))return "";
  return v.map(p=>p.type==="image"&&p.source
    ? `<a class="img" href="${esc(p.source.path)}" target="_blank" rel="noreferrer">
         <img loading="lazy" src="${esc(p.source.path)}" alt="${esc(p.source.media_type||"image")}"></a>`
    : text(p.text||"")).join("");
}

function step(s,ctx,depth,idx,prefix){
  // Subagent trajectories number their own steps from 1, so scope their DOM ids
  // by trajectory to keep every anchor on the page unique.
  const key=(prefix||"")+s.step_id;
  let h=`<div class="step ${s.source}" id="step-${key}">
    <div class="role"><a class="sid" href="#step-${key}" title="step ${s.step_id}">${s.step_id}</a>
    <b>${esc(s.source)}</b>`;
  if(s.timestamp)h+=`<span>${esc(s.timestamp.replace("T"," ").replace(/\.\d+Z?$/,""))}</span>`;
  if(s.model_name&&s.source==="agent")h+=`<span>${esc(s.model_name)}</span>`;
  if(s.metrics?.completion_tokens)h+=`<span>${num(s.metrics.completion_tokens)} tok</span>`;
  h+=`</div>`;
  if(s.reasoning_content)h+=`<div class="think">${esc(s.reasoning_content)}</div>`;
  if(s.message)h+=body(s.message);
  for(const c of s.tool_calls||[]){
    const r=(s.observation?.results||[]).find(x=>x.source_call_id===c.tool_call_id);
    const refs=r?.subagent_trajectory_ref||[];
    h+=`<details class="tool"><summary><span class="tname">${esc(c.function_name)}</span>
        ${refs.length?`<span class="pill br">delegates</span>`:""}</summary>
      <div class="tbody">
        <pre class="json">${hjson(c.arguments)}</pre>
        ${r?.content?(typeof r.content==="string"?pre(r.content):body(r.content)):""}
      </div></details>`;
    for(const ref of refs)h+=branch(ref,ctx,depth);
  }
  return h+`</div>`;
}

/* A delegated subagent is its own complete trajectory. Render it collapsed, and
   recurse — ATIF nests arbitrarily deep. */
function branch(ref,ctx,depth){
  const sub=(ctx.subagent_trajectories||[]).find(x=>x.trajectory_id===ref.trajectory_id);
  if(!sub){
    const where=ref.trajectory_path?`external file ${esc(ref.trajectory_path)}`:"not embedded";
    return `<div class="branch"><div class="miss">⤷ subagent ${esc(ref.trajectory_id||"")} — ${where}</div></div>`;
  }
  const x=sub.extra||{};
  const label=x.agentType||sub.agent?.name||"subagent";
  return `<details class="branch"><summary>
      <b>${esc(label)}</b>
      <span class="desc">${esc(x.description||"")}</span>
      <span class="pill br">${sub.steps.length} steps</span>
      ${depth?`<span class="pill">depth ${depth+1}</span>`:""}
    </summary><div class="inner">
      ${sub.steps.map(s=>step(s,sub,depth+1,null,esc(sub.trajectory_id||"sub")+"-")).join("")}
    </div></details>`;
}
</script>
"""


def _point_images_at_server(trajectory: Trajectory, index: int) -> None:
    """Rewrite relative image paths to a URL this server can answer.

    On disk an image path resolves next to the trajectory file, but nothing is
    written to disk here — the bytes are in memory, so they need an endpoint.
    """

    def parts(value):
        return [p for p in value if isinstance(p, ContentPart)] if isinstance(value, list) else []

    def walk(t: Trajectory) -> None:
        for step in t.steps:
            targets = list(parts(step.message))
            for result in (step.observation.results if step.observation else ()):
                targets += parts(result.content)
            for part in targets:
                if part.type == "image" and part.source and not part.source.path.startswith(
                    ("http://", "https://", "data:")
                ):
                    name = part.source.path.rsplit("/", 1)[-1]
                    part.source.path = f"/api/image?i={index}&name={name}"
        for sub in t.subagent_trajectories or ():
            walk(sub)

    walk(trajectory)


def _reveal(target: Path) -> bool:
    """Show a path in the OS file manager.

    Reveal-only, never open: `open -R` selects the item in Finder rather than
    launching whatever application is registered for it, so clicking a path in
    a log cannot execute anything.
    """
    if not target.exists():
        return False
    if sys.platform == "darwin":
        command = ["open", "-R", str(target)]
    elif sys.platform.startswith("linux") and shutil.which("xdg-open"):
        # xdg-open has no reveal equivalent, so open the containing directory.
        command = ["xdg-open", str(target if target.is_dir() else target.parent)]
    else:
        return False
    try:
        subprocess.run(command, check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


class _Handler(BaseHTTPRequestHandler):
    entries: list[Entry] = []
    cache: dict[int, dict] = {}
    media: dict[int, dict] = {}
    lock = threading.Lock()

    def log_message(self, *args):  # keep the console clean
        pass

    def _send(self, body: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        url = urlparse(self.path)

        if url.path == "/":
            self._send(PAGE.encode(), "text/html; charset=utf-8")
            return

        if url.path == "/api/index":
            rows = [
                {
                    "path": e.path,
                    "agent": e.agent,
                    "format": e.format,
                    "session_id": e.session_id,
                    "project": e.project,
                    "modified": e.modified,
                    "size_bytes": e.size_bytes,
                    "subagents": e.subagents,
                }
                for e in self.entries
            ]
            self._send(json.dumps(rows).encode(), "application/json")
            return

        if url.path == "/api/reveal":
            raw = (parse_qs(url.query).get("path") or [""])[0]
            if not raw:
                self.send_error(400)
                return
            target = Path(raw).expanduser()
            if _reveal(target):
                self._send(b"ok", "text/plain")
            else:
                self.send_error(404)
            return

        if url.path == "/api/image":
            query = parse_qs(url.query)
            try:
                index = int(query.get("i", ["-1"])[0])
            except ValueError:
                self.send_error(404)
                return
            name = (query.get("name") or [""])[0]
            with self.lock:
                item = self.media.get(index, {}).get(name)
            if item is None:
                self.send_error(404)
                return
            self._send(item.data, item.media_type)
            return

        if url.path == "/api/trajectory":
            try:
                index = int(parse_qs(url.query).get("i", ["-1"])[0])
                entry = self.entries[index]
            except (ValueError, IndexError):
                self._send(json.dumps({"error": "no such session"}).encode(), "application/json")
                return

            with self.lock:
                cached = self.cache.get(index)
            if cached is None:
                try:
                    trajectory, _ = convert(Path(entry.path), entry.format)
                    store = trajectory.all_media()
                    if store:
                        _point_images_at_server(trajectory, index)
                        with self.lock:
                            self.media[index] = dict(store.items)
                    cached = trajectory.to_dict()
                except Exception as exc:  # a bad log should not kill the server
                    self._send(
                        json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode(),
                        "application/json",
                    )
                    return
                with self.lock:
                    self.cache[index] = cached
            self._send(json.dumps(cached).encode(), "application/json")
            return

        self.send_error(404)


def serve(entries: list[Entry] | None = None, port: int = 7433, open_browser: bool = True) -> None:
    handler = partial(_Handler)
    _Handler.entries = entries if entries is not None else scan()
    _Handler.cache = {}
    _Handler.media = {}

    # Loopback only: these logs contain source code and tool output.
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"atif-viewer: {url}  ({len(_Handler.entries)} sessions)")
    print("Ctrl-C to stop.")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        server.server_close()
