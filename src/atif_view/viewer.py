"""Local trajectory viewer.

Serves a single self-contained page on loopback. Trajectories are converted on
demand and cached in memory, so opening the viewer over a large corpus is cheap
and only the sessions actually opened pay conversion cost.

The server binds 127.0.0.1 only — it reads local session logs, which routinely
contain source code and credentials in tool output, and must never be reachable
off-host.
"""

from __future__ import annotations

import errno
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import webbrowser
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from atif_make.atif import ContentPart, Trajectory
from atif_make import corpus
from atif_make.archive import extract, is_archive
from atif_make.convert import convert
from atif_make.corpus import Entry, scan

from . import ai, config, library

PAGE = r"""<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ATIF-View</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,400;6..72,600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
/* Diwan's design tokens, verbatim from www/frontend/src/index.css.
   Three themes, not two: `paper` and `cool` are both light. */
:root,:root[data-theme="paper"]{
  --bg:#f6f2ea; --surface:#fffdf8; --panel:#faf6ee; --ink:#241f18;
  --muted:#7d7466; --line:#e5ddcd; --accent:#b0522a;
  --soft:rgba(176,82,42,.09); --hl:rgba(233,180,76,.3); --ok:#3f7d4e;
  --shadow:0 1px 2px rgba(60,45,20,.08), 0 8px 28px rgba(60,45,20,.1);
}
:root[data-theme="cool"]{
  --bg:#f1f4f6; --surface:#ffffff; --panel:#f7fafb; --ink:#171c22;
  --muted:#68737f; --line:#dbe2e9; --accent:#33689f;
  --soft:rgba(51,104,159,.09); --hl:rgba(116,169,222,.28); --ok:#2f7a52;
  --shadow:0 1px 2px rgba(20,40,60,.08), 0 8px 28px rgba(20,40,60,.1);
}
:root[data-theme="dark"]{
  --bg:#18161c; --surface:#232028; --panel:#1d1b22; --ink:#eae4d8;
  --muted:#948d80; --line:#332f3a; --accent:#d3894b;
  --soft:rgba(211,137,75,.13); --hl:rgba(211,137,75,.25); --ok:#7fbf8f;
  --shadow:0 1px 2px rgba(0,0,0,.4), 0 10px 30px rgba(0,0,0,.45);
}
:root{
  --danger:oklch(0.54 0.13 30);
  --font-ui:"Helvetica Neue",Helvetica,Arial,sans-serif;
  --font-serif:Newsreader,Georgia,serif;
  --font-mono:"IBM Plex Mono",ui-monospace,monospace;
  /* Roles the trajectory view needs that Diwan has no token for. Derived from
     its palette so they stay in family across all three themes. */
  --dim:var(--muted); --faint:var(--muted); --sunk:var(--bg);
  --user:var(--accent); --agent:var(--ink); --system:var(--muted); --tool:var(--accent);
  --user-bg:var(--soft); --agent-bg:transparent; --system-bg:transparent;
  --j-key:var(--accent); --j-str:var(--ink); --j-num:var(--muted); --j-lit:var(--muted);
}
*{box-sizing:border-box;margin:0}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline;text-underline-offset:2px}
a.gone{color:var(--faint);text-decoration:line-through}
body{font:14px/1.55 var(--font-ui);background:var(--bg);color:var(--ink);display:flex;flex-direction:column;height:100vh;overflow:hidden}
#shell{flex:1;display:flex;min-height:0}
kbd,code,pre{font-family:var(--font-mono)}

/* ---- sidebar ---- */
#side{width:320px;flex:0 0 320px;border-right:1px solid var(--line);background:var(--panel);display:flex;flex-direction:column}
/* Diwan's header: 0 18px, one line, bordered below, on --panel. */
#top{display:flex;align-items:center;gap:18px;padding:0 18px;height:52px;
  border-bottom:1px solid var(--line);background:var(--panel);flex:none}
#top .mark{font:600 18px var(--font-serif);letter-spacing:-.01em;color:var(--ink)}
#crumb{flex:1;min-width:0;display:flex;align-items:baseline;gap:10px;overflow:hidden;
  font-size:13px;color:var(--muted)}
#crumb b{font-weight:500;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#crumb .back{cursor:pointer;color:var(--accent)}
#theme{font:500 11px var(--font-mono);letter-spacing:.05em;color:var(--muted);
  background:var(--surface);border:1px solid var(--line);border-radius:8px;
  padding:7px 10px;cursor:pointer;flex:none}
#theme:hover{color:var(--ink)}
#brand{padding:13px 14px 9px;display:flex;align-items:center;gap:9px}\n#brand .mark{font:600 17px var(--font-serif);letter-spacing:-.01em;color:var(--ink)}\n#brand small{font:500 10.5px var(--font-mono);color:var(--muted)}
#brand small{font-weight:400;color:var(--faint);font-size:11.5px}
#themes{margin-left:auto;display:inline-flex;gap:1px;border:1px solid var(--line);border-radius:20px;padding:1px;background:var(--surface)}
#themes span{width:15px;height:15px;border-radius:50%;cursor:pointer;border:2px solid transparent}
#themes span.on{border-color:var(--accent)}
#open{font:500 11px var(--font-mono);letter-spacing:.05em;color:var(--muted);
  background:var(--surface);border:1px solid var(--line);border-radius:8px;
  padding:7px 10px;cursor:pointer;flex:none}
#open:hover{color:var(--accent);border-color:var(--accent)}
#drop{position:fixed;inset:0;z-index:100;background:color-mix(in srgb,var(--bg) 88%,transparent);
  display:none;align-items:center;justify-content:center;font-size:16px;color:var(--accent);
  border:3px dashed var(--accent);pointer-events:none}
#drop.on{display:flex}
.note{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);z-index:200;max-width:70ch;
  background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--tool);
  border-radius:9px;padding:10px 14px;font-size:12.5px;white-space:pre-wrap;box-shadow:0 4px 14px rgba(0,0,0,.16)}
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
#toggle{position:fixed;top:62px;left:176px;z-index:20;width:28px;height:28px;padding:0;
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
/* ---- collection rail (Diwan's collectionTree, flattened) ---- */
#side{width:212px;flex:0 0 212px}
#tagbar{padding:0 14px 10px;display:flex;gap:4px;flex-wrap:wrap}
.rail{display:flex;align-items:center;gap:6px;padding:5px 12px 5px 10px;cursor:pointer;
  font-size:12.5px;border-left:2px solid transparent;color:var(--ink)}
.rail:hover{background:var(--bg)}
.rail.on{background:var(--soft);border-left-color:var(--accent);color:var(--accent)}
.rail .rl{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rail .rc{font:500 10px var(--font-mono);color:var(--muted)}
.caret{width:10px;display:inline-flex;color:var(--muted);opacity:.75;
  transition:transform .12s ease}
.caret.open{transform:rotate(90deg)}
.pill.tag{background:var(--soft);color:var(--accent);border:none;border-radius:20px;
  padding:1px 8px;font-size:10.5px;cursor:pointer;display:inline-flex;gap:5px;align-items:baseline}
.pill.tag b{font:600 9.5px var(--font-mono);opacity:.75}
.pill.tag.on{background:var(--accent);color:var(--surface)}
.pill.origin{border:1px solid var(--line);color:var(--muted);border-radius:20px;
  padding:1px 8px;font:500 10px var(--font-mono);cursor:pointer;background:transparent}
.pill.origin.on{background:var(--accent);color:var(--surface);border-color:var(--accent)}

/* ---- library table (Diwan's LibraryRow grid) ---- */
.lhead{display:flex;align-items:baseline;gap:12px;margin-bottom:14px}
.lhead h2{font:600 19px var(--font-serif);letter-spacing:-.01em}
.hd h2{cursor:text}
.inline.hdin{font:600 19px var(--font-serif);width:min(100%,34ch);padding:1px 7px}
.mono{font:500 10.5px var(--font-mono);color:var(--muted)}
.tgrid{display:grid;
  grid-template-columns:34px minmax(180px,2.4fr) 88px 84px 68px minmax(90px,1fr) 76px;
  gap:10px;align-items:center}
.thead{padding:6px 10px;border-bottom:1px solid var(--line);
  font:600 9.5px var(--font-mono);letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.trow{padding:10px;border-bottom:1px solid var(--line);cursor:pointer}
.trow:hover{background:var(--panel)}
.trow:hover .acts{opacity:1}
.tcell{min-width:0;overflow:hidden}
.tt{font-size:13px;font-weight:500;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;display:block}
.tags{display:flex;gap:4px;flex-wrap:nowrap;overflow:hidden}
.marks{display:flex;gap:4px;align-items:center;color:var(--muted)}
.marks .star{color:var(--accent)}
.inline{border:1px solid var(--accent);border-radius:5px;background:var(--surface);
  color:var(--ink);padding:2px 6px;font:inherit;font-size:13px;width:100%}
.inline.tagin{border-radius:20px;font-size:11px;width:86px}
.acts{display:flex;gap:1px;opacity:0;transition:opacity .12s ease}
.acts button{background:none;border:none;color:var(--muted);cursor:pointer;
  font-size:12px;padding:2px 4px;border-radius:5px;line-height:1}
.acts button:hover{color:var(--accent);background:var(--soft)}

/* Settings, over the whole page */
#modal{position:fixed;inset:0;background:rgba(0,0,0,.34);display:flex;
  align-items:center;justify-content:center;z-index:50;padding:24px}
#modal[hidden]{display:none}
.sheet{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  box-shadow:var(--shadow);padding:22px 24px;width:min(520px,100%)}
.sheet h2{font:600 15px var(--font-ui);margin:0 0 14px}
.sheet label{display:block;font:600 9.5px var(--font-mono);letter-spacing:.11em;
  text-transform:uppercase;color:var(--muted);margin-bottom:5px}
.sheet input{width:100%;padding:8px 11px;border:1px solid var(--line);border-radius:5px;
  background:var(--surface);color:var(--ink);font:13px var(--font-mono)}
.sheet input:focus{outline:none;border-color:var(--accent)}
.sheet .fine{font-size:11.5px;color:var(--muted);line-height:1.55;margin:8px 0 0}
.sheet .fine code{font:11px var(--font-mono);color:var(--ink)}
.sheet .fine.bad{color:var(--danger)}
.sheet .fine.warn{color:var(--ink)}
.sheet .row{display:flex;gap:8px;margin-top:16px}
.sheet .row button{border:1px solid var(--line);background:var(--surface);color:var(--ink);
  border-radius:5px;padding:6px 14px;font:inherit;font-size:12.5px;cursor:pointer}
.sheet .row button:hover{background:var(--sunk)}
.sheet .row button.primary{background:var(--accent);border-color:var(--accent);color:var(--bg)}
#gear{font:500 11px var(--font-mono);letter-spacing:.05em;color:var(--muted);
  background:none;border:none;cursor:pointer;padding:0 4px}
#gear:hover{color:var(--ink)}

/* The per-transcript switch */
.aisw{display:inline-flex;align-items:center;gap:6px;font:500 10px var(--font-mono);
  letter-spacing:.05em;color:var(--muted);cursor:pointer;user-select:none}
.aisw input{accent-color:var(--accent);margin:0}

/* Claude-backed explanations, offered only when configured */
.aibtn{margin-top:8px;border:1px solid var(--line);background:var(--surface);color:var(--muted);
  border-radius:20px;padding:3px 11px;font:500 10.5px var(--font-mono);letter-spacing:.05em;
  cursor:pointer}
.aibtn:hover{color:var(--accent);border-color:var(--accent)}
.aiout{margin-top:8px;border-left:2px solid var(--accent);background:var(--soft);
  border-radius:0 7px 7px 0;padding:8px 12px;font-size:13px}
.aiout.busy{color:var(--muted);border-left-color:var(--line);background:transparent}
.aiout.bad{border-left-color:var(--danger);color:var(--danger);background:transparent}
.ail{display:block;font:600 9px var(--font-mono);letter-spacing:.11em;text-transform:uppercase;
  color:var(--accent);margin-bottom:3px}
.ask{margin-bottom:16px;display:flex;flex-direction:column;gap:7px;align-items:flex-start}
.ask .askin,.ask .askout{width:100%}
.askin{width:100%;padding:8px 12px;border:1px solid var(--line);border-radius:9px;
  background:var(--surface);color:var(--ink);font:inherit;font-size:13.5px}
.askin:focus{outline:2px solid var(--accent);outline-offset:-1px}
.askout{margin-top:9px;border:1px solid var(--line);border-left:2px solid var(--accent);
  border-radius:0 9px 9px 0;padding:11px 14px;font-size:13.5px;background:var(--panel)}
.askout.bad{border-left-color:var(--danger);color:var(--danger)}
.askref{margin-top:7px;font:500 10px var(--font-mono);letter-spacing:.05em;color:var(--muted)}

/* tabs */
.tabs{display:flex;gap:2px;border-bottom:1px solid var(--line);margin:0 0 18px}
.tab{padding:7px 14px;font-size:13px;color:var(--dim);cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px}
.tab:hover{color:var(--ink)}
.tab.on{color:var(--ink);border-bottom-color:var(--accent);font-weight:600}

/* search + lenses + provenance */
.runbar{display:grid;grid-template-columns:1fr;gap:12px;margin-bottom:16px}
.search{width:100%;padding:9px 12px;border:1px solid var(--line);border-radius:9px;
  background:var(--panel);color:var(--ink);font:inherit;font-size:13.5px}
.search:focus{outline:2px solid var(--accent);outline-offset:-1px}
.lenses{display:flex;gap:5px;flex-wrap:wrap}
.lens{display:inline-flex;align-items:baseline;gap:6px;border:1px solid var(--line);
  border-radius:999px;padding:3px 6px 3px 11px;font-size:12px;color:var(--dim);cursor:pointer;background:var(--panel)}
.lens:hover{color:var(--ink)}
.lens.on{background:var(--ink);color:var(--bg);border-color:var(--ink)}
.lens b{font-variant-numeric:tabular-nums;font-size:11px;background:var(--sunk);color:var(--dim);
  border-radius:999px;padding:0 6px;font-weight:600}
.lens.on b{background:rgba(255,255,255,.2);color:var(--bg)}
.details{display:grid;grid-template-columns:auto minmax(0,1fr) auto minmax(0,1fr);
  gap:3px 10px;align-items:baseline;border-top:1px solid var(--line);padding-top:11px}
.dt{font:600 9.5px var(--font-mono);letter-spacing:.09em;text-transform:uppercase;
  color:var(--muted);white-space:nowrap;text-align:right}
.dd{font:400 11.5px var(--font-mono);color:var(--ink);min-width:0;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.dd a{color:var(--accent)}
@media(max-width:900px){.details{grid-template-columns:auto minmax(0,1fr)}}
.hits{font-size:12px;color:var(--dim);align-self:center;margin-left:4px}

/* raw + files panels */
.rawmeta{font-size:11.5px;color:var(--faint);margin-bottom:8px;overflow-wrap:anywhere}
.rawsrc{background:var(--sunk);border-radius:9px;padding:12px;font-size:11.5px;line-height:1.5;
  max-height:70vh;overflow:auto;white-space:pre;overflow-wrap:normal}
table.files{border-collapse:collapse;width:100%;font-size:13px}
table.files th{text-align:left;font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;
  color:var(--faint);font-weight:600;padding:6px 10px;border-bottom:1px solid var(--line)}
table.files td{padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:baseline}
table.files tr:hover td{background:var(--sunk)}

.filters{display:flex;gap:6px;margin-bottom:18px;flex-wrap:wrap;align-items:center}
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
/* The gutter is one column so the number and the star cannot drift apart:
   positioning them separately meant two sets of offsets to keep in step, and
   a padding nudge in a flex-end box moves content the wrong way. */
.gut{position:absolute;left:-52px;top:9px;width:38px;display:flex;flex-direction:column;
  align-items:flex-end;gap:7px}
.sid{font-variant-numeric:tabular-nums;font-size:11.5px;font-weight:600;letter-spacing:0;
  line-height:1.35;color:var(--faint);text-decoration:none}
.sid:hover{color:var(--accent)}
/* Always visible: an affordance revealed on hover is one nobody finds. Quiet
   at rest, accent once it means something. */
.sstar{display:flex;color:var(--muted);opacity:.45;cursor:pointer;
  transition:opacity .12s ease,color .12s ease}
.step:hover .sstar{opacity:.75}
.sstar:hover{opacity:1;color:var(--accent)}
.sstar.on,.step:hover .sstar.on{opacity:1;color:var(--accent)}
.branch .inner .gut{left:-40px;width:28px}
.branch .inner .sid{font-size:11px}
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
/* shell output: what changed, what failed, what passed */
.s-meta{color:var(--muted)}
.s-add{color:var(--ok)}
.s-del{color:var(--danger)}
.s-bad{color:var(--danger);font-weight:500}
.s-ok{color:var(--ok)}

/* JSON syntax colours */
.j-key{color:var(--j-key)}
.j-str{color:var(--j-str)}
.j-num{color:var(--j-num)}
.j-lit{color:var(--j-lit)}
pre.json{line-height:1.45}

/* A call and its output are different things; showing them in identical boxes
   made a long argument list read as though it were already output. */
.io{position:relative;border-radius:7px;margin-top:8px;padding:6px 10px 8px}
.io.in{background:var(--soft)}
.io.out{background:var(--bg);border:1px solid var(--line)}
.io .iol{display:block;font:600 9px var(--font-mono);letter-spacing:.11em;
  text-transform:uppercase;margin-bottom:2px}
.io.in .iol{color:var(--accent)}
.io.out .iol{color:var(--muted)}
.io pre{background:transparent;padding:0;margin-top:2px;max-height:320px}
.img{display:inline-block;margin:8px 8px 0 0}
.img img{max-width:min(420px,100%);max-height:300px;border:1px solid var(--line);border-radius:8px;display:block;background:var(--sunk)}
.more{padding:20px 0;text-align:center}
.more button{border:1px solid var(--line);background:var(--panel);color:var(--dim);border-radius:999px;
  padding:8px 18px;font:inherit;font-size:12.5px;cursor:pointer}
.more button:hover{background:var(--sunk);color:var(--ink)}
</style>

<header id="top">
  <span class="mark">ATIF-View</span>
  <div id="crumb"></div>
  <button id="theme" onclick="cycleTheme()" title="Change theme"></button>
  <button id="gear" onclick="openSettings()" title="Settings">Settings</button>
  <button id="open" onclick="picker.click()" title="Open a log, trajectory or archive">Open…</button>
</header>

<div id="modal" hidden onclick="if(event.target===this)closeSettings()">
  <div class="sheet">
    <h2>Settings</h2>
    <label for="akey">Anthropic API key</label>
    <input id="akey" type="password" spellcheck="false" autocomplete="off"
           placeholder="sk-ant-…" onkeydown="if(event.key==='Enter')saveKey()">
    <p class="fine" id="keystate"></p>
    <p class="fine">Stored at <code>~/.atif/config.json</code>, readable only by you.
       It is sent to the Anthropic API and nowhere else, and is never read back into
       this page. A key in your keychain or a password manager is safer than one in a
       file — set <code>ANTHROPIC_API_KEY</code> instead and leave this empty.</p>
    <div class="row">
      <button class="primary" onclick="saveKey()">Save key</button>
      <button onclick="clearKey()">Remove</button>
      <button onclick="closeSettings()">Close</button>
    </div>
    <p class="fine bad" id="keyerr" hidden></p>
  </div>
</div>
<div id="shell">
<div id="side">
  <div id="brand"><small id="count"></small></div>
  <input id="picker" type="file" multiple hidden
         accept=".jsonl,.json,.har,.zip,.gz,.tgz,.bz2,.xz,.tar"
         onchange="openFiles(this.files)">
  <div id="drop">Drop logs, trajectories or archives</div>
  <input id="q" placeholder="Filter…" oninput="drawList();if(!cur)showLibrary()">
  <div id="tagbar"></div>
  <div id="list"></div>
</div>
<button id="toggle" onclick="toggleSide()" title="Show/hide sessions (\\)" aria-label="Toggle sidebar">&lsaquo;</button>
<div id="main"><div class="empty">Loading…</div></div>
</div>

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
  // `label` is inserted as-is, so callers pass text they have already escaped.
const pathAnchor = (path, label) =>
  `<a href="/api/reveal?path=${encodeURIComponent(path)}" onclick="return reveal(this)"` +
  ` title="Reveal in Finder">${label === undefined ? path : label}</a>`;
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

/* Shell output gets scanned for three things: what changed, what failed, what
   passed. Measured over a real corpus those cover most of what appears — diffs
   8%, errors 8%, test results 14% — so colour those and leave the rest alone
   rather than tinting every line. */
function shell(text) {
  return escText(text)
    .split("\n")
    .map((line) => {
      const t = line.trimStart();
      // Headers first: "---" and "+++" would otherwise read as removed/added.
      if (/^(diff --git |index [0-9a-f]|@@ |\+\+\+ |--- )/.test(t))
        return `<span class="s-meta">${line}</span>`;
      if (/^\+/.test(t)) return `<span class="s-add">${line}</span>`;
      if (/^-(?!-)/.test(t)) return `<span class="s-del">${line}</span>`;
      if (/^(Traceback \(|FAILED\b|ERROR\b|fatal:|E\s{3})/.test(t))
        return `<span class="s-bad">${line}</span>`;
      if (/^(ok\b|PASSED\b)|\b\d+ passed\b|\bAll tests passed\b/.test(t))
        return `<span class="s-ok">${line}</span>`;
      return line;
    })
    .join("\n");
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
  return `<pre>${autolink(shell(content))}</pre>`;
}

const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const num=n=>(n??0).toLocaleString();
const short=s=>{const p=String(s||"").split("/").filter(Boolean);return p.slice(-2).join("/")||s};
const PAGE_SIZE=250;
let INDEX=[],FOLDERS=[],TAGS=[],AI={},ASKED=null,cur=null,traj=null,
    collection="__all",ACTIVE_TAGS={},ONLY_OPENED=false,EDITING=null,
    EXPANDED=(()=>{try{return JSON.parse(localStorage.getItem("atif-view.folders"))||{}}catch(e){return {}}})(),show={user:1,agent:1,system:1},onlyBranches=false,limit=PAGE_SIZE,raw=false;
let tab="trajectory",query="",lens="all",extra=null,RENAMING=false,OPEN_ALL=false;

function reveal(a){
  fetch(a.getAttribute("href")).then(r=>{
    // A path recorded in an old log may simply not exist any more.
    if(!r.ok)a.classList.add("gone");
  });
  return false;
}

/* Send a file to the server, which routes it through the same discovery the
   CLI uses. One request per file; the body is the file itself, so there is no
   multipart parsing to get wrong. */
async function openFiles(files){
  if(!files||!files.length)return;
  const names=[...files].map(f=>f.name);
  count.textContent=`opening ${names.length} file${names.length>1?"s":""}…`;
  const problems=[];
  let first=null;
  for(const file of files){
    try{
      const res=await fetch("/api/open",{method:"POST",
        headers:{"X-Filename":encodeURIComponent(file.name)},body:file});
      const data=await res.json();
      if(!res.ok||data.error){problems.push(`${file.name}: ${data.error||res.status}`);continue}
      if(first===null)first=(data.keys||[])[0]||null;
    }catch(err){problems.push(`${file.name}: ${err.message}`)}
  }
  const fresh=await fetch("/api/index").then(r=>r.json());
  INDEX=fresh.sessions||[];FOLDERS=fresh.folders||[];TAGS=fresh.tags||[];AI=fresh.ai||{};
  count.textContent=INDEX.length+" sessions";drawList();
  if(first!==null)pick(first);
  if(problems.length)note(problems.join("\n"));
  picker.value="";
}

function note(message){
  const el=document.createElement("div");
  el.className="note";el.textContent=message;
  document.body.appendChild(el);
  setTimeout(()=>el.remove(),6000);
}

// Dropping a file anywhere is the same action as choosing one. dragenter and
// dragleave fire for every element the pointer crosses, so count them instead
// of trying to detect the boundary from a single event.
let dragDepth=0;
const hasFiles=e=>[...(e.dataTransfer?.types||[])].includes("Files");
const showDrop=on=>{dragDepth=on?dragDepth:0;drop.classList.toggle("on",on)};

addEventListener("dragenter",e=>{
  if(!hasFiles(e))return;
  e.preventDefault();dragDepth++;drop.classList.add("on");
});
addEventListener("dragover",e=>{if(hasFiles(e))e.preventDefault()});
addEventListener("dragleave",e=>{
  if(!hasFiles(e))return;
  dragDepth=Math.max(0,dragDepth-1);
  if(!dragDepth)showDrop(false);
});
addEventListener("drop",e=>{
  e.preventDefault();showDrop(false);
  if(e.dataTransfer?.files?.length)openFiles(e.dataTransfer.files);
});
// A drag that ends outside the window never fires drop; do not strand the overlay.
addEventListener("dragend",()=>showDrop(false));
addEventListener("mouseout",e=>{if(!e.relatedTarget&&dragDepth)showDrop(false)});

const THEMES=["paper","cool","dark"];
const THEME_LABEL={paper:"PAPER",cool:"COOL",dark:"DARK"};

function setTheme(name){
  document.documentElement.setAttribute("data-theme",name);
  try{localStorage.setItem("atif-view.theme",name)}catch(e){}
  if(typeof theme!=="undefined"&&theme)theme.textContent=THEME_LABEL[name]||name;
}

function cycleTheme(){
  const now=document.documentElement.getAttribute("data-theme")||"paper";
  setTheme(THEMES[(THEMES.indexOf(now)+1)%THEMES.length]);
}

// Restore the chosen theme before anything paints, so there is no flash.
try{setTheme(localStorage.getItem("atif-view.theme")||"paper")}catch(e){setTheme("paper")}

function toggleSide(){
  const hidden=document.body.classList.toggle("hide-side");
  toggle.innerHTML=hidden?"&rsaquo;":"&lsaquo;";
  try{localStorage.setItem("atif-view.side",hidden?"1":"0")}catch(e){}
}
// Restore the last choice; localStorage can throw in restricted contexts.
try{if(localStorage.getItem("atif-view.side")==="1")toggleSide()}catch(e){}

addEventListener("keydown",e=>{
  // Ignore the shortcut while typing in the filter box.
  if(e.key==="\\"&&e.target.tagName!=="INPUT"){e.preventDefault();toggleSide()}
});

fetch("/api/index").then(r=>r.json()).then(d=>{
  INDEX=d.sessions||[];FOLDERS=d.folders||[];TAGS=d.tags||[];AI=d.ai||{};
  count.textContent=INDEX.length+" sessions";drawList();showLibrary();
});
q.oninput=drawList;

/* Diwan's collectionTree builds a forest carrying depth, then flattens it
   against an expanded set. Same shape here, over "a/b/c" folder strings. */
function buildForest(folders){
  const nodes=folders.map(path=>({path,depth:path.split("/").length-1,
    name:path.split("/").pop()}));
  return nodes.sort((a,b)=>a.path.localeCompare(b.path));
}

function visibleRails(){
  const counts={};
  let unfiled=0;
  for(const e of INDEX){
    if(!e.folder){unfiled++;continue}
    // A session in Redwood/SOC2 counts toward Redwood too, as Diwan rolls up.
    const parts=e.folder.split("/");
    for(let i=1;i<=parts.length;i++){
      const p=parts.slice(0,i).join("/");
      counts[p]=(counts[p]||0)+1;
    }
  }
  const rows=[{path:"__all",name:"All",depth:0,count:INDEX.length}];
  for(const n of buildForest(FOLDERS)){
    const parent=n.path.split("/").slice(0,-1).join("/");
    if(parent&&!EXPANDED[parent])continue;   // hidden under a collapsed parent
    rows.push({...n,count:counts[n.path]||0,
      children:FOLDERS.some(f=>f.startsWith(n.path+"/"))});
  }
  rows.push({path:"__unfiled",name:"Unfiled",depth:0,count:unfiled});
  return rows;
}

function drawList(){
  const rows=visibleRails();
  list.innerHTML=rows.map(r=>{
    const on=collection===r.path;
    const caret=r.children
      ? `<span class="caret ${EXPANDED[r.path]?"open":""}"
           onclick="event.stopPropagation();toggleFolder('${esc(r.path)}')">
           <svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="currentColor"
             stroke-width="3" stroke-linecap="round"><path d="m9 18 6-6-6-6"/></svg></span>`
      : `<span class="caret"></span>`;
    return `<div class="rail${on?" on":""}" onclick="setCollection('${esc(r.path)}')"
        style="padding-left:${10+r.depth*13}px">
        ${caret}<span class="rl">${esc(r.name)}</span>
        <span class="rc">${r.count}</span></div>`;
  }).join("");

  tagbar.innerHTML=TAGS.map(t=>{
    const on=!!ACTIVE_TAGS[t.name];
    return `<span class="pill tag${on?" on":""}" onclick="toggleTag('${esc(t.name)}')">
      ${esc(t.name)}<b>${t.count}</b></span>`;
  }).join("")+(INDEX.some(e=>e.origin==="opened")
    ? `<span class="pill origin${ONLY_OPENED?" on":""}" onclick="toggleOpened()"
         title="Opened by you, rather than found on this machine">opened</span>` : "");
}

const setCollection=p=>{collection=p;cur=null;drawList();showLibrary()};

function drawCrumb(){
  if(!crumb)return;
  const here=collection==="__all"?"All sessions"
    :collection==="__unfiled"?"Unfiled":collection;
  crumb.innerHTML=cur
    ? `<span class="back" onclick="showLibrary()">${esc(here)}</span>
       <span>/</span><b>${esc(titleOf(INDEX.find(e=>e.key===cur)||{}))}</b>`
    : `<b>${esc(here)}</b>`;
}
const toggleFolder=p=>{EXPANDED[p]=!EXPANDED[p];
  try{localStorage.setItem("atif-view.folders",JSON.stringify(EXPANDED))}catch(e){}
  drawList()};
const toggleTag=t=>{ACTIVE_TAGS[t]=!ACTIVE_TAGS[t];drawList();if(!cur)showLibrary()};
const toggleOpened=()=>{ONLY_OPENED=!ONLY_OPENED;drawList();if(!cur)showLibrary()};

function libraryRows(){
  const f=q.value.toLowerCase();
  const active=Object.keys(ACTIVE_TAGS).filter(t=>ACTIVE_TAGS[t]);
  return INDEX.filter(e=>{
    if(collection==="__unfiled"&&e.folder)return false;
    if(collection!=="__all"&&collection!=="__unfiled"
       &&!(e.folder===collection||e.folder.startsWith(collection+"/")))return false;
    if(ONLY_OPENED&&e.origin!=="opened")return false;
    if(active.length&&!active.every(t=>(e.tags||[]).includes(t)))return false;
    if(f){
      const hay=(e.title||"")+" "+(e.project||"")+" "+(e.session_id||"")+" "
        +e.agent+" "+(e.tags||[]).join(" ");
      if(!hay.toLowerCase().includes(f))return false;
    }
    return true;
  });
}

const titleOf=e=>e.title||short(e.project||e.session_id||e.path);

/* Diwan shares one grid template between the header and every row so the
   columns line up; the same trick, with the columns this corpus has. */
function showLibrary(){
  cur=null;drawCrumb();
  const rows=libraryRows();
  main.innerHTML=`
    <div class="lhead">
      <h2>${esc(collection==="__all"?"All sessions"
        :collection==="__unfiled"?"Unfiled":collection)}</h2>
      <span class="mono">${num(rows.length)} of ${num(INDEX.length)}</span>
    </div>
    <div class="tgrid thead">
      <span></span><span>Title</span><span>Agent</span>
      <span>Modified</span><span>Size</span><span>Tags</span><span></span>
    </div>
    <div id="tbody">${rows.map(libraryRow).join("")
      ||`<div class="empty">Nothing here yet.</div>`}</div>`;
}

function libraryRow(e){
  const editing=EDITING&&EDITING.key===e.key?EDITING.field:null;
  const title=editing==="title"
    ? `<input class="inline" value="${esc(e.title||titleOf(e))}" autofocus
         onkeydown="titleKey(event,'${e.key}')" onblur="stopEdit()">`
    : `<span class="tt" onclick="titleClick(event,'${e.key}')"
         ondblclick="startEdit('${e.key}','title')">${esc(titleOf(e))}</span>`;
  return `<div class="tgrid trow" onclick="openRow(event,'${e.key}')">
    <span class="marks">
      ${e.starred?`<svg class="star" width="11" height="11" viewBox="0 0 24 24" fill="currentColor"><path d="m12 2 3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/></svg>`:""}
      ${e.origin==="opened"?`<span class="opened" title="Opened by you, not found on this machine">
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
          <path d="M12 3v12"/><path d="m7 10 5 5 5-5"/>
          <path d="M4 18v1a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-1"/></svg></span>`:""}
    </span>
    <span class="tcell">${title}</span>
    <span class="mono">${esc(e.agent)}</span>
    <span class="mono">${e.modified?e.modified.slice(0,10):"—"}</span>
    <span class="mono">${(e.size_bytes/1048576).toFixed(1)} MB</span>
    <span class="tcell tags">${(e.tags||[]).map(t=>`<span class="pill tag">${esc(t)}</span>`).join("")}
      ${editing==="tags"?`<input class="inline tagin" placeholder="add tag"
         onkeydown="tagKey(event,'${e.key}')" onblur="stopEdit()" autofocus>`:""}</span>
    <span class="acts">
      <button title="Rename" onclick="event.stopPropagation();startEdit('${e.key}','title')">✎</button>
      <button title="File into…" onclick="event.stopPropagation();fileInto('${e.key}')">⊞</button>
      <button title="Tags" onclick="event.stopPropagation();startEdit('${e.key}','tags')">◌</button>
      <button title="Star" onclick="event.stopPropagation();star('${e.key}')">★</button>
    </span></div>`;
}

function openRow(event,key){
  if(event.target.closest("input,button"))return;
  pick(key);
}

/* Diwan's LibraryRow pattern: a single click on the title opens after a beat,
   and a second click inside that beat cancels it and renames instead. Without
   this the title either swallowed the click or opened on the way to renaming. */
let CLICK_TIMER=null;
function titleClick(event,key){
  event.stopPropagation();
  if(CLICK_TIMER){clearTimeout(CLICK_TIMER);CLICK_TIMER=null;return}
  CLICK_TIMER=setTimeout(()=>{CLICK_TIMER=null;pick(key)},220);
}

// ---- annotation -------------------------------------------------------------
/* AI controls appear only when a credential is configured AND this transcript
   has not been switched off. The server enforces the same rule. */
const aiOn=()=>{
  if(!AI.available)return false;
  const rec=INDEX.find(x=>x.key===cur);
  return !rec||rec.ai!==false;
};

function openSettings(){
  const err=document.getElementById("keyerr");
  if(err){err.hidden=true;err.textContent=""}
  const box=document.getElementById("akey");
  if(box)box.value="";
  showKeyState();
  modal.hidden=false;
  if(box)box.focus();
}
const closeSettings=()=>{modal.hidden=true};

/* Two independent things must both be true — a credential and the SDK — so
   say which one is missing. Reporting only "unavailable" made a saved key look
   like a failed save. */
function showKeyState(){
  const el=document.getElementById("keystate");
  if(!el)return;
  const stored=AI.source==="settings"?`Key saved here (${AI.hint}).`
    :AI.source==="environment"?"Using ANTHROPIC_API_KEY from the environment."
    :"No key saved.";
  el.textContent=AI.available
    ?`${stored} AI features are on · model ${AI.model}`
    :`${stored} ${AI.reason||"AI features are off."}`;
  el.className=AI.available?"fine":"fine warn";
}

async function settings(body){
  const err=document.getElementById("keyerr");
  err.hidden=true;
  try{
    AI=await postJSON("/api/settings",body);
    document.getElementById("akey").value="";
    showKeyState();
    render();
  }catch(e){err.textContent=e.message;err.hidden=false}
}

const saveKey=()=>{
  const value=document.getElementById("akey").value.trim();
  if(value)settings({api_key:value});
};
const clearKey=()=>settings({clear:true});

/* Nothing here runs on its own: every request below is the direct result of a
   click, so a transcript's contents never leave the machine unasked. */
async function postJSON(url,body){
  const res=await fetch(url,{method:"POST",
    headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const data=await res.json().catch(()=>({error:"the server said nothing useful"}));
  if(!res.ok||data.error)throw new Error(data.error||`request failed (${res.status})`);
  return data;
}

const askClaude=body=>postJSON("/api/ai",{key:cur,...body});

async function explainCall(event,callId){
  event.preventDefault();event.stopPropagation();
  const box=document.getElementById("ai-"+callId);
  if(!box)return;
  box.hidden=false;
  box.className="aiout busy";
  box.textContent="Reading the call…";
  try{
    const {summary}=await askClaude({what:"call",call_id:callId});
    box.className="aiout";
    box.innerHTML=`<span class="ail">summary</span>${md(summary)}`;
  }catch(err){
    box.className="aiout bad";
    box.textContent=err.message;
  }
}

async function askSession(event){
  if(event.key!=="Enter")return;
  const question=event.target.value.trim();
  if(!question)return;
  ASKED={question,answer:"",steps:[],busy:true};
  render();
  try{
    const {answer,steps}=await askClaude({what:"ask",question});
    ASKED={question,answer,steps,busy:false};
  }catch(err){
    ASKED={question,answer:"",error:err.message,busy:false};
  }
  render();
}

async function annotate(key,fields){
  try{await postJSON("/api/library",{key,...fields})}
  catch(e){note("Could not save that change.");return}
  await refreshIndex();
}

async function refreshIndex(){
  const d=await fetch("/api/index").then(r=>r.json());
  INDEX=d.sessions||[];FOLDERS=d.folders||[];TAGS=d.tags||[];AI=d.ai||{};
  count.textContent=INDEX.length+" sessions";
  drawList();
  if(!cur)showLibrary();
}

const startEdit=(key,field)=>{
  if(CLICK_TIMER){clearTimeout(CLICK_TIMER);CLICK_TIMER=null}
  EDITING={key,field};showLibrary();
};
const stopEdit=()=>{EDITING=null;showLibrary()};

function titleKey(event,key){
  if(event.key==="Escape")return stopEdit();
  if(event.key!=="Enter")return;
  const value=event.target.value.trim();
  EDITING=null;
  annotate(key,{title:value});
}

function tagKey(event,key){
  if(event.key==="Escape")return stopEdit();
  if(event.key!=="Enter")return;
  const entry=INDEX.find(e=>e.key===key);
  const value=event.target.value.trim().toLowerCase();
  if(!value)return stopEdit();
  EDITING=null;
  annotate(key,{tags:[...(entry.tags||[]),value]});
}

function star(key){
  const entry=INDEX.find(e=>e.key===key);
  annotate(key,{starred:!entry.starred});
}

function fileInto(key){
  const entry=INDEX.find(e=>e.key===key);
  const value=prompt("File into which collection?  (blank to unfile)",entry.folder||"");
  if(value===null)return;
  annotate(key,{folder:value});
}

function pick(key){
  cur=key;EDITING=null;drawList();drawCrumb();
  main.innerHTML=`<div class="empty">Converting…</div>`;
  fetch("/api/trajectory?id="+encodeURIComponent(key)).then(r=>r.json()).then(t=>{
    if(t.error){main.innerHTML=`<div class="empty">${esc(t.error)}</div>`;return}
    traj=t;onlyBranches=false;limit=PAGE_SIZE;query="";lens="all";tab="trajectory";extra=null;RENAMING=false;render();
  });
}

const toggle=r=>{show[r]=!show[r];limit=PAGE_SIZE;render()};
const toggleBranches=()=>{onlyBranches=!onlyBranches;limit=PAGE_SIZE;render()};
const more=()=>{limit+=PAGE_SIZE*4;render()};
const toggleRaw=()=>{raw=!raw;render()};

/* Open or close every tool call and branch at once. Applied to the elements
   already on screen rather than by repainting: a repaint would drop the reader
   to the top, and the state is kept so a later filter change honours it. */
function toggleExpand(){
  OPEN_ALL=!OPEN_ALL;
  main.querySelectorAll("details.tool,details.branch").forEach(d=>{d.open=OPEN_ALL});
  const chip=document.getElementById("xall");
  if(chip){chip.textContent=OPEN_ALL?"collapse all":"expand all";
           chip.classList.toggle("on",OPEN_ALL)}
}
const setLens=l=>{lens=l;limit=PAGE_SIZE;render()};

function renameKey(event){
  if(event.key==="Escape"){RENAMING=false;return render()}
  if(event.key!=="Enter")return;
  const value=event.target.value.trim();
  RENAMING=false;
  annotate(cur,{title:value});
}
const setQuery=v=>{query=v;limit=PAGE_SIZE;render()};
function setTab(t){
  tab=t;
  // Raw and Files come from the server; fetch once per session, then cache.
  if(t!=="trajectory"&&!(extra&&extra[t])){
    fetch(`/api/${t}?id=${encodeURIComponent(cur)}`).then(r=>r.json()).then(d=>{extra={...extra,[t]:d};render()});
  }
  render();
}

/* What a step counts as, for the filter counts. A step can be several things
   at once: an agent turn that calls tools and carries reasoning. */
/* Starred steps live on the session's library record, keyed the same way the
   anchors are — plain id at the top level, trajectory-scoped inside a subagent,
   whose ids restart at 1. */
const stepKey = (step, prefix) => (prefix || "") + step.step_id;
const starredSteps = () =>
  (INDEX.find((e) => e.key === cur) || {}).starred_steps || [];
const isStarred = (step, prefix) => starredSteps().includes(stepKey(step, prefix));

async function toggleStep(event, key) {
  event.stopPropagation();
  // render() repaints the whole pane, which drops the reader back to the top
  // of a long transcript. Starring is a small act; hold their place.
  const at = main.scrollTop;
  const now = starredSteps();
  const next = now.includes(key) ? now.filter((k) => k !== key) : [...now, key];
  await annotate(cur, { starred_steps: next });
  render();
  main.scrollTop = at;
}

function facets(s){
  const f=[s.source];
  if(s.tool_calls?.length)f.push("tools");
  if(s.reasoning_content)f.push("reasoning");
  if(isStarred(s,""))f.push("favourited");
  return f;
}
const LENSES=[["all","All steps"],["user","User"],["agent","Agent"],["system","System"],
              ["tools","Tools"],["reasoning","Reasoning"],["branches","Branches"],
              ["favourited","Favourited"]];

/* Search the text a reader can actually see: message, reasoning, tool names and
   arguments, and observation content. */
function haystack(s){
  const parts=[typeof s.message==="string"?s.message:(s.message||[]).map(p=>p.text||"").join(" "),
               s.reasoning_content||""];
  for(const c of s.tool_calls||[]){parts.push(c.function_name);parts.push(JSON.stringify(c.arguments||{}))}
  for(const r of s.observation?.results||[]){
    if(typeof r.content==="string")parts.push(r.content);
  }
  return parts.join(" ").toLowerCase();
}

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
  const t=traj,e=INDEX.find(x=>x.key===cur)||{},m=t.final_metrics||{},a=t.agent||{};
  const branches=branchIndex(t);
  const branchSteps=new Set(branches.map(b=>b.stepIndex));
  const q=query.trim().toLowerCase();

  // Counts describe the whole run, so they stay stable as filters change.
  const counts={all:t.steps.length,branches:branches.length};
  for(const [k] of LENSES){ if(k!=="all"&&k!=="branches")counts[k]=0 }
  t.steps.forEach(s=>facets(s).forEach(f=>{if(f in counts)counts[f]++}));

  main.innerHTML=`
    <div class="hd">
      ${RENAMING
        ? `<input id="rn" class="inline hdin" value="${esc(titleOf(e))}"
             onkeydown="renameKey(event)" onblur="RENAMING=false;render()">`
        : `<h2 ondblclick="RENAMING=true;render()"
             title="Double-click to rename">${esc(titleOf(e))}</h2>`}
      <div class="sub">${esc(a.name)} ${esc(a.version||"")} · ${esc(a.model_name||"no model")} · ${esc(t.session_id||"")}</div>
    </div>
    <div class="stats">
      ${stat(t.steps.length,"steps")}
      ${(()=>{const d=duration(spanOf(t.steps));
         return d?`<div class="stat"><b>${d}</b><span>duration</span></div>`:""})()}
      ${stat(counts.tools,"tool turns")}
      ${stat(counts.reasoning,"reasoning")}
      ${branches.length?stat(branches.length,"branches"):""}
      ${stat(m.total_prompt_tokens,"prompt")}
      ${stat(m.total_completion_tokens,"output")}
      ${stat(m.total_cached_tokens,"cached")}
    </div>
    <div class="tabs">
      ${[["trajectory","Trajectory"],["raw","Raw"],["files","Files"]].map(([k,l])=>
        `<span class="tab${tab===k?" on":""}" onclick="setTab('${k}')">${l}</span>`).join("")}
    </div>
    ${tab==="trajectory"?trajectoryTab(t,e,branches,branchSteps,counts,q):panelTab()}`;

  const box=document.getElementById("q2");
  if(box){ box.value=query; box.focus();
           box.setSelectionRange(box.value.length,box.value.length); }
}

function trajectoryTab(t,e,branches,branchSteps,counts,q){
  let rows=t.steps.map((s,i)=>({s,i}));
  if(lens==="branches")rows=rows.filter(({i})=>branchSteps.has(i));
  else if(lens!=="all")rows=rows.filter(({s})=>facets(s).includes(lens));
  else rows=rows.filter(({s})=>show[s.source]);
  if(q)rows=rows.filter(({s})=>haystack(s).includes(q));

  const total=rows.length, shown=rows.slice(0,limit);
  return `
    <div class="runbar">
      <input id="q2" class="search" placeholder="Search this run…"
             oninput="setQuery(this.value)" spellcheck="false">
      <div class="lenses">
        ${LENSES.filter(([k])=>k!=="branches"||branches.length)
          .map(([k,l])=>(k==="favourited"?`<span class="fsep"></span>`:"")
            +`<span class="lens${lens===k?" on":""}" onclick="setLens('${k}')">
             ${l}<b>${num(counts[k]??0)}</b></span>`).join("")}
      </div>
      ${details(t,e)}
    </div>
    ${AI.available?aiStrip():""}
    ${branches.length&&lens!=="branches"?branchNav(branches):""}
    <div class="filters">
      ${lens==="all"?["user","agent","system"].map(r=>
        `<span class="chip${show[r]?" on":""}" onclick="toggle('${r}')">${r}</span>`).join(""):""}
      <span class="fsep"></span>
      <span class="chip${OPEN_ALL?" on":""}" id="xall" onclick="toggleExpand()">${
        OPEN_ALL?"collapse all":"expand all"}</span>
      <span class="chip${raw?" on":""}" onclick="toggleRaw()">raw text</span>
      ${q?`<span class="hits">${num(total)} of ${num(t.steps.length)} match “${esc(query)}”</span>`:""}
    </div>
    ${shown.map(({s,i})=>step(s,t,0,i,"")).join("")
      ||`<div class="empty">Nothing matches.</div>`}
    ${total>shown.length?`<div class="more"><button onclick="more()">Show more —
        ${num(shown.length)} of ${num(total)} steps</button></div>`:""}`;
}

/* The switch stays visible when AI is off for a transcript — otherwise there
   would be no way to turn it back on. Only the ask box hides. */
function aiStrip(){
  const on=aiOn();
  return `<div class="ask">
    <label class="aisw" title="Hide every AI control for this transcript">
      <input type="checkbox" ${on?"checked":""}
        onchange="annotate(cur,{ai:this.checked}).then(render)"> With AI support
    </label>
    ${on?askInner():""}
  </div>`;
}

/* Ask a question about this transcript. Nothing is sent until Enter. */
function askInner(){
  const a=ASKED;
  return `
    <input class="askin" placeholder="Ask about this transcript…" spellcheck="false"
      value="${a?esc(a.question):""}" onkeydown="askSession(event)">
    ${a?`<div class="askout${a.error?" bad":""}">
      ${a.busy?"Looking through the steps…"
        :a.error?esc(a.error)
        :`${md(a.answer)}<div class="askref">from ${a.steps.length} step${
            a.steps.length===1?"":"s"}</div>`}
    </div>`:""}`;
}

/* Provenance: what this trajectory is and where it came from. */
/* Provenance reads as a table, not a ragged run: flex-wrap put each label on
   its own line as soon as one value was long. */
function details(t,e){
  const rows=[
    ["Schema",esc(t.schema_version)],
    ["Source",esc(e.format)],
    ["Agent",esc([t.agent?.name,t.agent?.version].filter(Boolean).join(" "))],
    ["Model",esc(t.agent?.model_name)],
    ["Session",esc(t.session_id)],
    ["Size",e.size_bytes?(e.size_bytes/1048576).toFixed(1)+" MB":""],
    // The path is the one value worth acting on, so it reveals in the file
    // manager through the same helper the prose linkifier uses.
    ["Path",e.path?pathAnchor(e.path,esc(e.path)):""],
  ];
  return `<div class="details">${rows.filter(([,v])=>v).map(([k,v])=>
    `<div class="dt">${k}</div><div class="dd" title="${v.replace(/<[^>]*>/g,"")}">${v}</div>`
  ).join("")}</div>`;
}

function panelTab(){
  const d=extra&&extra[tab];
  if(!d)return `<div class="empty">Loading…</div>`;
  if(tab==="raw"){
    return `<div class="rawmeta">${esc(d.path)} · ${(d.size/1048576).toFixed(2)} MB${
      d.truncated?` · showing the first ${(RAW_KB)} KB`:""}</div>
      <pre class="rawsrc">${esc(d.text)}</pre>`;
  }
  if(!d.length)return `<div class="empty">No associated files.</div>`;
  return `<table class="files"><thead><tr><th>File</th><th>Role</th><th>Size</th></tr></thead><tbody>${
    d.map(f=>`<tr><td>${pathAnchor(f.path,esc(f.name))}</td>
      <td><span class="pill">${esc(f.role)}</span></td>
      <td>${(f.size/1024).toFixed(0)} KB</td></tr>`).join("")}</tbody></table>`;
}
const RAW_KB=512;
// Pretty-printing every line of a large log would balloon the page; enough to
// read the shape is the point of this panel.
const RAW_LINES=300;

/* Colour the source with hjson — the same function that colours a tool
   argument — rather than growing a second way to render JSON. A log is one
   object per line; a trajectory or HAR is one document. */
function rawSource(text){
  const lines=text.split("\n");
  const shown=lines.slice(0,RAW_LINES);
  const parsed=shown.map(l=>{
    if(!l.trim())return null;
    try{return {v:JSON.parse(l)}}catch(e){return undefined}
  });
  // A truncated fetch leaves a partial final line, which is expected — every
  // other line failing means this is not line-oriented.
  const solid=parsed.filter((p,i)=>i<parsed.length-1&&p!==null);
  if(solid.length&&solid.every(p=>p!==undefined)){
    const html=shown.map((l,i)=>{
      const p=parsed[i];
      return p===null?"":p===undefined?escText(l):hjson(p.v);
    }).filter(Boolean).join("\n\n");
    const rest=lines.length-shown.length;
    return {html,note:rest>0?` · first ${RAW_LINES} of ${num(lines.length)} records`:""};
  }
  try{return {html:hjson(JSON.parse(text)),note:""}}catch(e){}
  return {html:escText(text),note:""};      // not JSON at all
}

/* Elapsed time from the first step to the last. Sessions here run from seconds
   to weeks, so the unit follows the span rather than fixing on one. */
function spanOf(steps){
  const times=steps.map(s=>s.timestamp).filter(Boolean).map(t=>Date.parse(t))
    .filter(n=>!Number.isNaN(n));
  if(times.length<2)return null;
  const ms=Math.max(...times)-Math.min(...times);
  return ms>0?ms:null;
}

function duration(ms){
  if(ms==null)return null;
  // A whole number reads better without the decimal: 2h, not 2.0h.
  const trim=n=>(n<10?n.toFixed(1).replace(/\.0$/,""):String(Math.round(n)));
  const s=ms/1000;
  if(s<90)return Math.round(s)+"s";
  const m=s/60;
  if(m<90)return Math.round(m)+"m";
  const h=m/60;
  if(h<48)return trim(h)+"h";
  const d=h/24;
  if(d<14)return trim(d)+"d";
  const w=d/7;
  return trim(w)+"w";
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
  const on=starredSteps().includes(key);
  let h=`<div class="step ${s.source}" id="step-${key}">
    <div class="gut">
      <a class="sid" href="#step-${key}" title="step ${s.step_id}">${s.step_id}</a>
      <span class="sstar${on?" on":""}" onclick="toggleStep(event,'${key}')"
        title="${on?"Remove from favourites":"Add to favourites"}">
        <svg width="11" height="11" viewBox="0 0 24 24" fill="${on?"currentColor":"none"}"
          stroke="currentColor" stroke-width="2"><path d="m12 2 3.09 6.26L22 9.27l-5 4.87
          1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/></svg></span>
    </div>
    <div class="role"><b>${esc(s.source)}</b>`;
  if(s.timestamp)h+=`<span>${esc(s.timestamp.replace("T"," ").replace(/\.\d+Z?$/,""))}</span>`;
  if(s.model_name&&s.source==="agent")h+=`<span>${esc(s.model_name)}</span>`;
  if(s.metrics?.completion_tokens)h+=`<span>${num(s.metrics.completion_tokens)} tok</span>`;
  h+=`</div>`;
  if(s.reasoning_content)h+=`<div class="think">${esc(s.reasoning_content)}</div>`;
  if(s.message)h+=body(s.message);
  for(const c of s.tool_calls||[]){
    const r=(s.observation?.results||[]).find(x=>x.source_call_id===c.tool_call_id);
    const refs=r?.subagent_trajectory_ref||[];
    h+=`<details class="tool"${OPEN_ALL?" open":""}><summary><span class="tname">${esc(c.function_name)}</span>
        ${refs.length?`<span class="pill br">delegates</span>`:""}</summary>
      <div class="tbody">
        <div class="io in"><span class="iol">call</span>
          <pre class="json">${hjson(c.arguments)}</pre></div>
        ${r?.content?`<div class="io out"><span class="iol">output</span>
          ${typeof r.content==="string"?pre(r.content):body(r.content)}</div>`:""}
        ${aiOn()?`<button class="aibtn" onclick="explainCall(event,'${c.tool_call_id}')"
          title="Send this one call to Claude and explain it">explain this call</button>
          <div class="aiout" id="ai-${c.tool_call_id}" hidden></div>`:""}
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
  return `<details class="branch"${OPEN_ALL?" open":""}><summary>
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


def _point_images_at_server(trajectory: Trajectory, index: str) -> None:
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
                    part.source.path = f"/api/image?id={index}&name={name}"
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


# Enough of a log to inspect its shape without shipping a 143 MB file.
RAW_LIMIT = 512 * 1024

# A browser upload crosses loopback, so this is generous; it exists to stop a
# stray multi-gigabyte archive from filling the disk, not to be restrictive.
UPLOAD_LIMIT = 2 * 1024 * 1024 * 1024

# Where opened files live. atif-make owns the location because it also has to
# recognise one during a scan; a second constant here could drift from it.


def _ai_state() -> dict:
    """What the page may know about credentials: whether, and from where.

    Never the key itself — only a four-character tail, enough to tell two
    apart. A page that cannot read the key cannot leak it.
    """
    ok, reason = ai.status()
    return {
        "available": ok,
        "reason": reason,
        "source": config.source(),
        "hint": config.hint(),
        "model": ai.MODEL,
    }


def _safe_name(raw: str) -> str:
    """Reduce a client-supplied filename to a leaf, so it cannot escape."""
    name = Path(unquote(raw or "")).name.strip()
    return name or "upload"


def _associated_files(source: Path) -> list[dict]:
    """Everything that travelled with a session: subagents, sidecars, siblings.

    A session is rarely one file — Claude Code keeps subagent traces in a
    sibling directory, and a bundle carries images next to the trajectory.
    """
    found: list[dict] = []
    seen: set[Path] = set()

    def add(path: Path, role: str) -> None:
        resolved = path.resolve()
        if resolved in seen or not path.is_file():
            return
        seen.add(resolved)
        found.append({
            "name": path.name,
            "path": str(path),
            "role": role,
            "size": path.stat().st_size,
        })

    add(source, "source")
    subagents = source.parent / source.stem / "subagents"
    if subagents.is_dir():
        for file in sorted(subagents.iterdir()):
            add(file, "subagent")
    # A bundle keeps its images and manifest beside the trajectory.
    for sibling in sorted(source.parent.iterdir()):
        if sibling == source:
            continue
        if sibling.is_dir() and sibling.name == "images":
            for image in sorted(sibling.iterdir()):
                add(image, "image")
        elif sibling.is_file() and sibling.suffix in {".json", ".jsonl", ".har", ".md", ".txt"}:
            add(sibling, "sibling")
    return found


class _Handler(BaseHTTPRequestHandler):
    entries: list[Entry] = []
    cache: dict[str, dict] = {}
    media: dict[str, dict] = {}
    lock = threading.Lock()

    def log_message(self, *args):  # keep the console clean
        pass

    def _send(self, body: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _entry(self, url):
        """Resolve ?id=<content key> to an entry, or None.

        Addressing by list position broke the moment entries could be added,
        removed or reordered — a stale link would open a different session.
        """
        key = (parse_qs(url.query).get("id") or [""])[0]
        if not key:
            return None
        return next((e for e in self.entries if e.key == key), None)

    def do_DELETE(self) -> None:
        url = urlparse(self.path)
        if url.path != "/api/library":
            self.send_error(404)
            return
        key = (parse_qs(url.query).get("id") or [""])[0]
        if not key:
            self._json({"error": "a key is required"}, 400)
            return

        entry = next((e for e in self.entries if e.key == key), None)
        library.remove(key)

        # An opened file only exists because it was brought in; forgetting it
        # should not leave a stray copy on disk. A scanned file is not ours.
        removed_copy = False
        if entry is not None and entry.origin == "opened":
            source = Path(entry.path)
            if corpus.OPENED_ROOT in source.parents:
                # An unpacked archive puts several logs under one directory, so
                # clearing it wholesale would delete the siblings' files and
                # leave their index rows pointing at nothing.
                store = corpus.OPENED_ROOT / source.relative_to(corpus.OPENED_ROOT).parts[0]
                shares = any(
                    e.key != key and store in Path(e.path).parents for e in self.entries
                )
                if shares:
                    source.unlink(missing_ok=True)
                else:
                    shutil.rmtree(store, ignore_errors=True)
                removed_copy = True
            with self.lock:
                self.entries = [e for e in self.entries if e.key != key]
                _Handler.entries = self.entries
            corpus.save(self.entries)
        self._json({"removed": True, "removed_copy": removed_copy})

    def do_POST(self) -> None:
        url = urlparse(self.path)

        if url.path == "/api/settings":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                self._json({"error": "expected a JSON body"}, 400)
                return
            # The key is read out of the body and handed straight to storage:
            # not logged, not echoed, not kept in memory beyond this call.
            try:
                if body.get("clear"):
                    config.clear_api_key()
                elif "api_key" in body:
                    config.set_api_key(body.get("api_key") or "")
                else:
                    self._json({"error": "nothing to change"}, 400)
                    return
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
                return
            except OSError as exc:
                self._json({"error": f"could not save settings: {exc.strerror}"}, 500)
                return
            self._json(_ai_state())
            return

        if url.path == "/api/ai":
            if not ai.available():
                self._json({"error": "AI features are not configured here."}, 501)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                self._json({"error": "expected a JSON body"}, 400)
                return

            entry = next((e for e in self.entries if e.key == body.get("key")), None)
            if entry is None:
                self._json({"error": "no such session"}, 404)
                return
            if not library.get(entry.key).get("ai", True):
                self._json({"error": "AI is switched off for this transcript."}, 403)
                return
            trajectory = self._trajectory(entry)
            if trajectory is None:
                self._json({"error": "could not read that session"}, 500)
                return

            try:
                if body.get("what") == "call":
                    self._json(self._summarise_call(body, entry, trajectory))
                elif body.get("what") == "ask":
                    question = (body.get("question") or "").strip()
                    if not question:
                        self._json({"error": "ask what?"}, 400)
                        return
                    answer, used = ai.ask(question, trajectory.get("steps", []))
                    self._json({"answer": answer, "steps": used})
                else:
                    self._json({"error": "unknown request"}, 400)
            except ai.Unavailable as exc:
                self._json({"error": str(exc)}, 503)
            return

        if url.path == "/api/library":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                self._json({"error": "expected a JSON body"}, 400)
                return
            key = body.get("key")
            if not isinstance(key, str) or not key:
                self._json({"error": "a key is required"}, 400)
                return
            fields = {k: v for k, v in body.items() if k != "key"}
            try:
                record = library.update(key, **fields)
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
                return
            self._json({"key": key, **record})
            return

        if url.path != "/api/open":
            self.send_error(404)
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.send_error(400)
            return
        if length <= 0:
            self._json({"error": "empty upload"}, 400)
            return
        if length > UPLOAD_LIMIT:
            self._json({"error": f"file is larger than {UPLOAD_LIMIT // 1024 ** 3} GB"}, 413)
            return

        name = _safe_name(self.headers.get("X-Filename", ""))
        staging = Path(tempfile.mkdtemp(prefix="atif-open-"))
        staged = staging / name
        try:
            remaining = length
            with staged.open("wb") as handle:
                while remaining > 0:
                    chunk = self.rfile.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    handle.write(chunk)
                    remaining -= len(chunk)
        except OSError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            self._json({"error": f"could not save upload: {exc}"}, 500)
            return

        # Derive the identity before choosing where it lands, so re-opening the
        # same file updates its place instead of accumulating copies.
        home = corpus.OPENED_ROOT / corpus.content_key(staged)
        try:
            home.mkdir(parents=True, exist_ok=True)
            if is_archive(staged):
                # Keep what is inside, not the container: the logs are what get
                # indexed, and unpacking here keeps their paths stable and any
                # sibling images resolvable. Storing the zip would mean
                # re-extracting to a temp directory on every start.
                unpacked = extract(staged)
                for item in unpacked.iterdir():
                    shutil.move(str(item), home / item.name)
                target = home
            else:
                target = home / name
                shutil.move(str(staged), target)
        except (OSError, ValueError) as exc:
            shutil.rmtree(home, ignore_errors=True)
            self._json({"error": f"could not store upload: {exc}"}, 500)
            return
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        # Exactly what `atif-view <path>` and `atif-make index` do: the CLI and
        # this button must never disagree about what counts as openable.
        try:
            found = scan([target], origin="opened")
        except (ValueError, OSError) as exc:
            shutil.rmtree(home, ignore_errors=True)
            self._json({"error": str(exc)}, 400)
            return
        if not found:
            # Nothing usable in it — do not keep the copy around.
            shutil.rmtree(home, ignore_errors=True)
            self._json({"error": f"nothing convertible in {name}"}, 415)
            return

        with self.lock:
            merged = corpus.merge(self.entries, found)
            self.entries = merged
            _Handler.entries = merged
        # Persist so an opened file is present next start without a re-scan.
        corpus.save(merged)
        self._json({
            "added": len(found),
            "keys": [e.key for e in found],
            "names": [Path(e.path).name for e in found],
        })

    def _trajectory(self, entry) -> dict | None:
        """The converted trajectory for an entry, from cache when it is there."""
        with self.lock:
            cached = self.cache.get(entry.key)
        if cached is not None:
            return cached
        try:
            trajectory, _ = convert(Path(entry.path), entry.format)
        except Exception:
            return None
        payload = trajectory.to_dict()
        with self.lock:
            self.cache[entry.key] = payload
        return payload

    def _summarise_call(self, body: dict, entry, trajectory: dict) -> dict:
        """Explain one tool call, reusing a summary already paid for."""
        call_id = body.get("call_id")
        stored = library.get(entry.key).get("summaries") or {}
        if call_id in stored and not body.get("again"):
            return {"summary": stored[call_id], "cached": True}

        call = result = None
        for step in trajectory.get("steps", []):
            for candidate in step.get("tool_calls") or []:
                if candidate.get("tool_call_id") == call_id:
                    call = candidate
                    for row in (step.get("observation") or {}).get("results") or []:
                        if row.get("source_call_id") == call_id:
                            result = row.get("content")
                    break
        if call is None:
            return {"error": "no such call"}

        summary = ai.summarise_call(call, result)
        library.update(entry.key, summaries={**stored, call_id: summary})
        return {"summary": summary, "cached": False}

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
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
                    "key": e.key,
                    "origin": e.origin,
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
            payload = {
                "ai": _ai_state(),
                "sessions": library.decorate(rows),
                "folders": library.folders(),
                "tags": [{"name": t, "count": n} for t, n in library.tags()],
            }
            self._send(json.dumps(payload).encode(), "application/json")
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

        if url.path == "/api/raw":
            entry = self._entry(url)
            if entry is None:
                self.send_error(404)
                return
            source = Path(entry.path)
            try:
                # A rollout can be hundreds of MB; send a head, not the lot.
                with source.open("rb") as handle:
                    body = handle.read(RAW_LIMIT + 1)
            except OSError:
                self.send_error(404)
                return
            truncated = len(body) > RAW_LIMIT
            payload = {
                "path": str(source),
                "size": source.stat().st_size,
                "truncated": truncated,
                "text": body[:RAW_LIMIT].decode("utf-8", errors="replace"),
            }
            self._send(json.dumps(payload).encode(), "application/json")
            return

        if url.path == "/api/files":
            entry = self._entry(url)
            if entry is None:
                self.send_error(404)
                return
            self._send(json.dumps(_associated_files(Path(entry.path))).encode(),
                       "application/json")
            return

        if url.path == "/api/image":
            query = parse_qs(url.query)
            index = (query.get("id") or [""])[0]
            name = (query.get("name") or [""])[0]
            with self.lock:
                item = self.media.get(index, {}).get(name)
            if item is None:
                self.send_error(404)
                return
            self._send(item.data, item.media_type)
            return

        if url.path == "/api/trajectory":
            entry = self._entry(url)
            if entry is None:
                self._send(json.dumps({"error": "no such session"}).encode(), "application/json")
                return
            index = entry.key

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


def _bind(port: int, handler, explicit: bool) -> ThreadingHTTPServer:
    """Bind to `port`, or to the next free one when the choice was ours.

    Viewing a second session while a first is still open is normal, so a busy
    default should not be an error. An explicitly requested port is honoured or
    reported — silently moving it would be worse than failing.
    """
    last: OSError | None = None
    for candidate in range(port, port + (1 if explicit else 20)):
        try:
            return ThreadingHTTPServer(("127.0.0.1", candidate), handler)
        except OSError as exc:
            if exc.errno not in (errno.EADDRINUSE, errno.EACCES):
                raise
            last = exc
    raise SystemExit(
        f"atif-view: port {port} is already in use"
        + ("" if explicit else f" (tried {port}-{port + 19})")
        + ".\nPass --port to choose another, or stop the running viewer."
    ) from last


def serve(
    entries: list[Entry] | None = None,
    port: int = 7433,
    open_browser: bool = True,
    explicit_port: bool = False,
) -> None:
    handler = partial(_Handler)
    _Handler.entries = entries if entries is not None else scan()
    _Handler.cache = {}
    _Handler.media = {}

    # Loopback only: these logs contain source code and tool output.
    server = _bind(port, handler, explicit_port)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    print(f"atif-view: {url}  ({len(_Handler.entries)} sessions)")
    print("Ctrl-C to stop.")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        server.server_close()
