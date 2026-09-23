"""The dashboard UI: ONE self-contained HTML document.

WHY a single inlined string instead of templates or static files: the server
must work with zero extra dependencies and zero extra routes (no CDN, no
build step, no static directory to mis-configure), and the page is small
enough to read in one sitting. All data arrives over the JSON API, so the
HTML itself carries no secrets and no per-request templating.

Everything user-supplied (group names carry emoji, 💥, quotes) is inserted
with `textContent`/`createTextNode`, never `innerHTML`, so a hostile group
name cannot inject script into the operator's dashboard.
"""

PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>autoposter · control</title>
<style>
:root {
  --bg:#090c12; --panel:#10151f; --panel2:#151b28; --line:rgba(148,163,184,.10);
  --line2:rgba(148,163,184,.18); --ink:#dde4f0; --ink2:#97a3b9; --ink3:#67718a;
  --acc:#7aa2ff; --acc2:#43d6bd; --ok:#5cd68a; --warn:#eac76d; --bad:#ff6f87;
  --mono:ui-monospace,'Cascadia Mono','JetBrains Mono',Consolas,'Courier New',monospace;
}
* { box-sizing:border-box; }
html { color-scheme:dark; }
body {
  margin:0; padding:26px 22px 48px; color:var(--ink);
  font:14.5px/1.55 'Segoe UI','Inter',system-ui,-apple-system,sans-serif;
  background:
    radial-gradient(1100px 520px at 12% -12%, rgba(96,124,255,.11), transparent 60%),
    radial-gradient(900px 520px at 106% 6%, rgba(67,214,189,.07), transparent 55%),
    var(--bg);
  background-attachment:fixed;
}
::selection { background:rgba(122,162,255,.30); }
*::-webkit-scrollbar { width:9px; height:9px; }
*::-webkit-scrollbar-thumb { background:#27303f; border-radius:9px;
  border:2px solid transparent; background-clip:content-box; }
*::-webkit-scrollbar-thumb:hover { background:#334056; background-clip:content-box; }
*::-webkit-scrollbar-track { background:transparent; }
.wrap { max-width:1380px; margin:0 auto; }
.mono { font-family:var(--mono); font-size:.92em; color:var(--ink2); }
.note { color:var(--ink3); font-size:11.5px; margin-top:10px; }
.err, .note.err { color:var(--bad); }
a { color:var(--acc); }

/* ---------- header ---------- */
.head-flex { display:flex; gap:20px; align-items:flex-start; flex-wrap:wrap; }
.head-l { flex:1 1 560px; min-width:0; }
.brand { display:flex; align-items:center; gap:11px; flex-wrap:wrap; }
.logo { width:31px; height:31px; border-radius:9px; flex:none;
  background:linear-gradient(135deg,#6c8cff,#37c8b4); display:grid; place-items:center;
  font:800 13px/1 var(--mono); color:#0a0f1c; letter-spacing:-.5px;
  box-shadow:0 5px 16px rgba(96,124,255,.35), inset 0 1px 0 rgba(255,255,255,.35); }
h1 { font-size:16.5px; font-weight:650; letter-spacing:-.01em; margin:0;
  display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
h1 .sub { color:var(--ink3); font-weight:450; font-size:12.5px; letter-spacing:0; }
.row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-top:14px; }

/* ---------- pills ---------- */
.pill { display:inline-flex; align-items:center; gap:6px; padding:3px 10px;
  border-radius:99px; border:1px solid var(--line2); color:var(--ink2);
  background:rgba(148,163,184,.07); font-size:11.5px; font-weight:550;
  line-height:1.5; white-space:nowrap; }
.pill::before { content:""; width:6px; height:6px; border-radius:50%;
  background:currentColor; opacity:.85; flex:none; }
.pill.ok   { color:var(--ok);   border-color:rgba(92,214,138,.32);  background:rgba(92,214,138,.07); }
.pill.warn { color:var(--warn); border-color:rgba(234,199,109,.32); background:rgba(234,199,109,.07); }
.pill.bad  { color:var(--bad);  border-color:rgba(255,111,135,.32); background:rgba(255,111,135,.07); }
.pill.live { color:#ff93a6; border-color:rgba(255,111,135,.5);
  background:rgba(255,111,135,.10); animation:armed 1.6s ease-in-out infinite; }
@keyframes armed { 50% { box-shadow:0 0 0 4px rgba(255,111,135,.10); } }

/* ---------- controls ---------- */
.btnset { display:flex; gap:8px; align-items:center; padding:7px 9px;
  background:rgba(21,27,40,.7); border:1px solid var(--line); border-radius:13px;
  backdrop-filter:blur(4px); }
.btnset .lbl { font-size:9.5px; font-weight:700; text-transform:uppercase;
  letter-spacing:.12em; color:var(--ink3); padding:0 2px 0 3px; }
.btnset .div { width:1px; align-self:stretch; background:var(--line2); margin:2px 2px; }
button, input[type=text] { font:inherit; }
button { appearance:none; cursor:pointer; border-radius:9px; padding:7px 13px;
  font-size:12.5px; font-weight:600; color:var(--ink);
  border:1px solid var(--line2); background:var(--panel2);
  transition:transform .12s ease, border-color .12s, background .12s, box-shadow .12s; }
button:hover:not(:disabled) { border-color:rgba(122,162,255,.55);
  background:#1a2233; transform:translateY(-1px);
  box-shadow:0 6px 16px rgba(0,0,0,.30); }
button:active:not(:disabled) { transform:translateY(0); }
button:focus-visible { outline:2px solid var(--acc); outline-offset:2px; }
button:disabled { opacity:.38; cursor:default; }
button.danger { color:#ff93a6; border-color:rgba(255,111,135,.35);
  background:rgba(255,111,135,.08); }
button.danger:hover:not(:disabled) { border-color:rgba(255,111,135,.65);
  background:rgba(255,111,135,.14); }
button.ghost { background:transparent; border-color:var(--line); color:var(--ink2); }
button.ghost:hover:not(:disabled) { color:var(--ink); }
input[type=text] { background:#0b0f17; border:1px solid var(--line2);
  border-radius:9px; color:var(--ink); padding:7px 10px; width:128px;
  font-family:var(--mono); font-size:12.5px; }
input[type=text]:focus { outline:none; border-color:rgba(122,162,255,.6);
  box-shadow:0 0 0 3px rgba(122,162,255,.14); }
input[type=text]::placeholder { color:var(--ink3); }
.hint-box { flex:none; max-width:420px; text-align:right; font-size:11px;
  line-height:1.7; color:var(--ink3); margin-top:2px;
  background:linear-gradient(180deg, rgba(122,162,255,.06), rgba(148,163,184,.035));
  border:1px solid var(--line2); border-radius:12px; padding:11px 14px;
  backdrop-filter:blur(4px); box-shadow:inset 0 1px 0 rgba(255,255,255,.04); }
.hint-box b { color:var(--ink2); font-weight:650; }
.hint-box .ok { color:var(--ok); } .hint-box .warn { color:var(--warn); }

/* ---------- layout ---------- */
.grid { display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-top:16px; }
.span2 { grid-column:1 / -1; }
@media (max-width:1080px) { .grid { grid-template-columns:1fr; } }
.card { background:linear-gradient(180deg,var(--panel2),var(--panel));
  border:1px solid var(--line); border-radius:15px; padding:17px 19px;
  box-shadow:0 10px 28px rgba(0,0,0,.30), inset 0 1px 0 rgba(255,255,255,.03); }
.card h2 { margin:0 0 13px; font-size:10.5px; font-weight:700;
  text-transform:uppercase; letter-spacing:.13em; color:var(--ink2);
  display:flex; align-items:center; gap:9px; }
.card h2::before { content:""; width:3px; height:13px; border-radius:2px; flex:none;
  background:linear-gradient(180deg,var(--acc),var(--acc2)); }

/* ---------- stats ---------- */
.stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(126px,1fr)); gap:10px; }
.stats > div { background:rgba(9,12,18,.45); border:1px solid var(--line);
  border-radius:12px; padding:13px 16px 12px; }
.stats b { display:block; font-size:21px; font-weight:650; letter-spacing:-.02em;
  font-variant-numeric:tabular-nums; line-height:1.15; }
.stats span { font-size:9.5px; font-weight:650; text-transform:uppercase;
  letter-spacing:.09em; color:var(--ink3); }
.stats > div:nth-child(1) b { color:var(--acc); }
.stats > div:nth-child(2) b { color:var(--ok); }
.stats > div:nth-child(4) b { color:var(--bad); }

/* ---------- tables ---------- */
.tablewrap { overflow:auto; max-height:400px; margin:0 -6px; }
table { width:100%; border-collapse:separate; border-spacing:0; font-size:12.8px; }
thead th { position:sticky; top:0; z-index:1; text-align:left; padding:8px 11px;
  background:#131926; border-bottom:1px solid var(--line2);
  font-size:10px; font-weight:700; text-transform:uppercase; letter-spacing:.09em;
  color:var(--ink3); white-space:nowrap; }
thead th.num { text-align:right; }
tbody td { padding:8.5px 11px; border-bottom:1px solid rgba(148,163,184,.055);
  white-space:nowrap; }
tbody td.name { white-space:normal; max-width:330px; }
tbody tr:hover td { background:rgba(122,162,255,.055); }
tbody tr:last-child td { border-bottom:none; }
td.num { text-align:right; font-variant-numeric:tabular-nums; }
th.num, td.num { min-width:46px; }
td.dim { color:var(--ink3); } td.ok { color:var(--ok); } td.warn { color:var(--warn); }
td .pill { font-size:10.5px; padding:2px 9px; }
.empty { text-align:center; color:var(--ink3); font-style:italic; padding:20px !important; }
.next-cell { color:var(--ok); font-weight:700; }

/* ---------- content editor ---------- */
.editorbar { display:flex; gap:10px; align-items:center; margin-top:11px;
  flex-wrap:wrap; }
.editorbar .note { margin:0; }
.ptext { width:100%; min-height:230px; background:#0b0f17;
  border:1px solid var(--line2); border-radius:12px; color:var(--ink);
  font:13px/1.65 var(--mono); padding:13px 15px; resize:vertical; }
.ptext:focus { outline:none; border-color:rgba(122,162,255,.6);
  box-shadow:0 0 0 3px rgba(122,162,255,.13); }
.cars { display:grid; grid-template-columns:repeat(auto-fill,minmax(320px,1fr));
  gap:12px; }
.car { background:rgba(9,12,18,.45); border:1px solid var(--line);
  border-radius:12px; padding:12px 13px; cursor:pointer;
  transition:border-color .15s ease, box-shadow .15s ease; }
.car.paste-target { border-color:rgba(122,162,255,.55);
  box-shadow:0 0 0 3px rgba(122,162,255,.12); }
.ptbadge { margin-left:8px; font:600 9.5px var(--mono); letter-spacing:.08em;
  text-transform:uppercase; color:var(--acc);
  border:1px solid rgba(122,162,255,.45); border-radius:99px; padding:1px 7px;
  vertical-align:1px; }
.car h3 { margin:0 0 10px; font:650 12px/1.4 var(--mono); color:var(--ink2); }
.thumbs { display:flex; flex-wrap:wrap; gap:10px; min-height:72px;
  align-items:flex-start; }
.thumb { position:relative; width:96px; }
.thumb img { width:96px; height:70px; object-fit:cover; display:block;
  border-radius:9px; border:1px solid var(--line); background:#0b0f17; }
.thumb button { position:absolute; top:-7px; right:-7px; width:22px;
  height:22px; padding:0; border-radius:50%; display:none; font-size:12px;
  line-height:1; }
.thumb:hover button { display:inline-block; }
.carfoot { display:flex; gap:8px; align-items:center; margin-top:11px;
  flex-wrap:wrap; }
input[type=file] { font-size:11.5px; color:var(--ink3); max-width:180px; }

/* ---------- log pane ---------- */
pre#log { background:#070a10; border:1px solid var(--line); border-radius:12px;
  padding:13px 15px; margin:0; height:320px; overflow:auto;
  font:12.3px/1.65 var(--mono); color:#9fb2ca; white-space:pre-wrap;
  word-break:break-word; }
label[for=autoscroll] { font-size:12px; color:var(--ink2); }
</style>
</head>
<body>
<div class="wrap">

<header class="head-flex">
  <div class="head-l">
    <div class="brand">
      <div class="logo">ap</div>
      <h1>autoposter <span class="sub">local control</span></h1>
      <span class="pill" id="ident"></span>
      <span class="pill" id="mode"></span>
    </div>
    <div class="row" id="controls">
      <span class="btnset"><span class="lbl">test</span>
        <button id="b-dry-groups">Dry run · groups</button>
        <button id="b-dry-cross">Dry run · crosspost</button>
      </span>
      <span class="btnset"><span class="lbl">data</span>
        <button class="ghost" id="b-refresh-groups">Refresh groups</button>
        <button class="ghost" id="b-refresh-listings">Refresh listings</button>
      </span>
      <span class="btnset"><span class="lbl">live</span>
        <input type="text" id="confirm" placeholder="type PUBLICAR"
               autocomplete="off" spellcheck="false">
        <button class="danger" id="b-live-groups">LIVE · groups</button>
        <button class="danger" id="b-live-cross">LIVE · crosspost</button>
      </span>
      <span class="btnset">
        <button class="danger" id="b-kill" disabled>Kill run</button>
        <span class="div"></span>
        <span class="pill" id="runstate">idle</span>
      </span>
    </div>
    <div class="note err" id="action"></div>
  </div>
  <div class="hint-box" id="hint">
    live buttons: type <b>PUBLICAR</b> in the box <b>AND</b> set
    <b>AP_DRY_RUN=false</b> in .env<br>
    <b>dry</b> = test run: composer staged/screenshot, nothing published ·
    <b>rc</b> = run exit code (0 ok · 1 nothing posted · 2 login · 3 identity)<br>
    <b>live?</b> = after a publish: <span class="ok">✓ visible</span> in the feed ·
    <span class="warn">⏳ pending</span> admin review · ❔ couldn't tell
  </div>
</header>

<div class="grid">

  <div class="card span2">
    <h2>Status</h2>
    <div class="stats" id="stats"></div>
    <div class="note" id="statusnote"></div>
  </div>

  <div class="card span2">
    <h2>Post text · data/post.txt</h2>
    <textarea class="ptext" id="ptext" spellcheck="false"
      placeholder="no post.txt yet — write it here; saving creates it"></textarea>
    <div class="editorbar">
      <button id="b-save-post">Save post text</button>
      <button class="ghost" id="b-reload-post">Reload from disk</button>
      <span class="note" id="ptnote"></span>
    </div>
    <div class="note">Copied VERBATIM into every group post (line breaks and
      emoji included) — this editor is the only place to change it; no need to
      touch the file by hand.</div>
  </div>

  <div class="card span2">
    <h2>Car photos · data/car_photos</h2>
    <div class="cars" id="cars"></div>
    <div class="editorbar">
      <input type="text" id="newcar" placeholder="05_new_folder"
             autocomplete="off" spellcheck="false">
      <button class="ghost" id="b-addcar">Add car folder</button>
      <span class="note" id="carnote">click a folder to make it the
        <b>paste target</b>, then <b>Ctrl+V</b> drops clipboard images straight
        into it · folder order = car order in the post · uploads need the
        folder to exist first</span>
    </div>
  </div>

  <div class="card span2">
    <h2>Joined groups &amp; rotation</h2>
    <div class="tablewrap"><table id="groups">
      <thead><tr><th>next</th><th>group id</th><th>name</th><th>last attempt</th>
        <th>status</th><th>live?</th><th class="num">pub.</th><th class="num">att.</th></tr></thead>
      <tbody></tbody>
    </table></div>
    <div class="note" id="groupsnote"></div>
  </div>

  <div class="card">
    <h2>Marketplace listings</h2>
    <div class="tablewrap"><table id="listings">
      <thead><tr><th>id</th><th>title</th><th>price</th><th>approved</th>
        <th>rejected</th><th class="num">batches</th><th class="num">groups</th><th>live?</th></tr></thead>
      <tbody></tbody>
    </table></div>
    <div class="note" id="listingsnote"></div>
  </div>

  <div class="card">
    <h2>Run logs on disk</h2>
    <div class="tablewrap"><table id="logs">
      <thead><tr><th>file</th><th>modified</th><th class="num">bytes</th></tr></thead>
      <tbody></tbody>
    </table></div>
  </div>

  <div class="card span2">
    <h2>Recent runs (ledger)</h2>
    <div class="tablewrap"><table id="runs">
      <thead><tr><th>run id</th><th>last event</th><th>kinds</th><th>dry</th>
        <th class="num">pub</th><th class="num">staged</th><th class="num">failed</th>
        <th class="num">skipped</th><th class="num">crossposts</th></tr></thead>
      <tbody></tbody>
    </table></div>
  </div>

  <div class="card span2">
    <h2>Live output</h2>
    <div class="row" style="margin:0 0 10px">
      <label><input type="checkbox" id="autoscroll" checked> autoscroll</label>
      <button class="ghost" id="b-clear">Clear</button>
      <span class="note mono" id="logcursor" style="margin:0 auto 0 0"></span>
    </div>
    <pre id="log"></pre>
    <div class="note">Streamed from the running CLI subprocess (stdout+stderr
      merged). Dry runs stage the composer and close it without publishing.</div>
  </div>

</div>
</div>

<script>
"use strict";
const $ = (id) => document.getElementById(id);
let cursor = 0;          // next /api/log index to request
let busy = false;

function text(node, s) { node.textContent = (s === null || s === undefined) ? "" : String(s); }

function td(row, value, cls) {
  const cell = document.createElement("td");
  if (cls) cell.className = cls;
  text(cell, value);
  row.appendChild(cell);
  return cell;
}

function fillTable(tableId, rows, build, emptyMsg) {
  const body = document.querySelector("#" + tableId + " tbody");
  body.replaceChildren();
  if (!rows.length) {
    const tr = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 12; cell.className = "empty"; text(cell, emptyMsg);
    tr.appendChild(cell); body.appendChild(tr);
    return;
  }
  for (const item of rows) {
    const tr = document.createElement("tr");
    build(item, tr);
    body.appendChild(tr);
  }
}

function pill(value, klass) {
  const s = document.createElement("span");
  s.className = "pill " + (klass || "");
  text(s, value);
  return s;
}

function statusClass(status) {
  if (status === "published") return "ok";
  if (status === "staged") return "warn";
  if (status === "failed") return "bad";
  if (status === "skipped") return "warn";
  return "";
}

const DELIVERED = { live: ["✓ visible", "ok"], pending: ["⏳ pending", "warn"],
                    unknown: ["❔ unknown", "dim"] };

function deliveredCell(tr, row) {
  const dv = DELIVERED[row.delivered] || ["—", "dim"];
  td(tr, dv[0]).className = dv[1];
}

function fmtTs(ts) {
  if (!ts) return "never";
  return ts.replace("T", " ").slice(0, 16);
}

function statBox(value, label) {
  const d = document.createElement("div");
  const b = document.createElement("b"); text(b, value);
  const s = document.createElement("span"); text(s, label);
  d.appendChild(b); d.appendChild(s);
  return d;
}

function renderState(st) {
  const ident = st.identity || {};
  const envDry = ident.env_dry_run !== false;
  text($("ident"), "as " + (ident.label || "?") + (ident.post_as ? " (" + ident.post_as + ")" : ""));
  $("mode").className = "pill " + (envDry ? "warn" : "live");
  text($("mode"), envDry ? "AP_DRY_RUN=true (live blocked by .env)" : "AP_DRY_RUN=false — live armed");

  const g = st.groups || {}, l = st.listings || {}, led = st.ledger || {};
  const rot = st.rotation || {}, nodes = st.manager || {};
  const stats = $("stats"); stats.replaceChildren();
  stats.appendChild(statBox(rot.total || 0, "joined groups"));
  const sc = led.status_counts || {};
  stats.appendChild(statBox(led.published_total || 0, "published all-time"));
  stats.appendChild(statBox(sc.staged || 0, "staged (dry)"));
  stats.appendChild(statBox((sc.failed || 0) + (sc.skipped || 0), "failed+skipped"));
  stats.appendChild(statBox(led.crosspost_lines || 0, "crosspost lines"));
  stats.appendChild(statBox(led.lines || 0, "ledger lines"));
  stats.appendChild(statBox(rot.planned ? rot.planned.length : 0, "next run targets"));

  text($("statusnote"),
    "capture root: " + ((st.paths || {}).capture_root || "?") +
    " · run cap: " + (ident.max_posts_per_run || 0) + " per run · state built " +
    (st.generated_at || ""));

  // groups table
  fillTable("groups", g.rows || [], (row, tr) => {
    const nx = td(tr, row.next ? "▶" : "");
    if (row.next) nx.className = "next-cell";
    td(tr, row.id, "mono");
    td(tr, row.name, "name");
    td(tr, fmtTs(row.last_ts), "mono");
    const cell = td(tr, row.last_status || "never");
    cell.className = "pill " + statusClass(row.last_status);
    deliveredCell(tr, row);
    td(tr, row.published, "num");
    td(tr, row.attempts, "num");
  }, g.available ? "no joined groups in this snapshot (confirmed empty)"
                 : "not fetched yet — run Refresh groups");
  text($("groupsnote"),
    g.available
      ? ("snapshot " + fmtTs(g.fetched_at) + " · " + g.count + " groups · " +
         (rot.never_attempted || []).length + " never attempted · source " +
         (g.source || ""))
      : "not fetched yet — run Refresh groups (opens the browser, needs the profile unlocked)");

  // listings table
  fillTable("listings", l.rows || [], (row, tr) => {
    td(tr, row.id, "mono");
    td(tr, row.title);
    td(tr, row.price, "mono");
    td(tr, row.approved === null || row.approved === undefined ? "—" : (row.approved ? "yes" : "no"));
    td(tr, row.rejected === null || row.rejected === undefined ? "—" : (row.rejected ? "yes" : "no"));
    td(tr, row.crossposts, "num");
    td(tr, row.crosspost_groups + (row.crosspost_coverage === null ? "" : " (" + row.crosspost_coverage + "%)"), "num");
    deliveredCell(tr, row);
  }, l.available ? "listings snapshot is empty (confirmed 0 active listings)"
                 : "not fetched yet — no listings.json on disk");
  text($("listingsnote"),
    l.available ? ("snapshot " + fmtTs(l.fetched_at) + " · " + l.count + " active listings")
                : "not fetched yet — press Refresh listings to fetch it");

  // runs table
  fillTable("runs", (st.runs || {}).recent || [], (row, tr) => {
    td(tr, row.run_id, "mono");
    td(tr, fmtTs(row.last_ts), "mono");
    td(tr, (row.kinds || []).join("+"));
    td(tr, row.dry_run === null ? "?" : (row.dry_run ? "yes" : "NO"));
    td(tr, row.published, "num");
    td(tr, row.staged, "num");
    td(tr, row.failed, "num");
    td(tr, row.skipped, "num");
    td(tr, row.crossposts, "num");
  }, "no runs recorded in the ledger yet");

  // logs table
  fillTable("logs", (st.runs || {}).logs || [], (row, tr) => {
    td(tr, row.name, "mono");
    td(tr, fmtTs(row.mtime), "mono");
    td(tr, row.size, "num");
  }, "no run logs yet");

  renderManager(st);
  renderButtons(st);
}

function renderManager(st) {
  const m = st.manager || {};
  const el = $("runstate");
  if (m.running) {
    el.className = "pill " + (m.live ? "live" : "warn");
    text(el, (m.live ? "LIVE " : "dry ") + (m.mode || "") + " · pid " + m.pid);
  } else {
    el.className = "pill";
    text(el, m.rc === null || m.rc === undefined ? "idle"
         : "idle · last rc=" + m.rc);
  }
}

function renderButtons(st) {
  const m = st.manager || {};
  const avail = (st.modes || {});
  busy = !!m.running;
  ["b-dry-groups", "b-dry-cross", "b-live-groups", "b-live-cross",
   "b-refresh-groups"].forEach((id) => { $(id).disabled = busy; });
  if (avail.crosspost === false) {
    $("b-dry-cross").disabled = true;
    $("b-dry-cross").title = "poster.crosspost is not on this checkout";
    $("b-live-cross").disabled = true;
    $("b-live-cross").title = "poster.crosspost is not on this checkout";
  }
  $("b-refresh-listings").disabled = avail["listings-refresh"] !== true;
  $("b-kill").disabled = !busy;
  const envDry = (st.identity || {}).env_dry_run !== false;
  if (envDry) {
    $("b-live-groups").disabled = true;
    $("b-live-cross").disabled = true;
    $("b-live-groups").title = "AP_DRY_RUN=true in .env — edit it to arm live runs";
    $("b-live-cross").title = "AP_DRY_RUN=true in .env — edit it to arm live runs";
  }
}

async function post(url, body) {
  const opts = { method: "POST" };
  if (body) {
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(url, opts);
  let data = {};
  try { data = await resp.json(); } catch (e) { data = {}; }
  if (!resp.ok) throw new Error(data.error || ("HTTP " + resp.status));
  return data;
}

function note(message) {
  // Messages from actions land in their own line: the instructions above must
  // stay readable after the first click.
  const target = $("action");
  target.replaceChildren();
  const span = document.createElement("span");
  span.textContent = message;
  target.appendChild(span);
}

function run(mode, live) {
  const payload = { mode: mode, live: !!live };
  if (live) payload.confirm = $("confirm").value;
  post("/api/run", payload)
    .then((d) => { note("started " + mode + (live ? " LIVE" : " (dry)") +
                        " · pid " + d.pid); poll(); })
    .catch((e) => note("refused: " + e.message));
}

$("b-dry-groups").onclick = () => run("groups", false);
$("b-dry-cross").onclick = () => run("crosspost", false);
$("b-live-groups").onclick = () => run("groups", true);
$("b-live-cross").onclick = () => run("crosspost", true);
$("b-refresh-groups").onclick = () => {
  post("/api/refresh-groups").then(() => poll())
    .catch((e) => note("refresh refused: " + e.message));
};
$("b-refresh-listings").onclick = () => {
  post("/api/refresh-listings").then(() => poll())
    .catch((e) => note("refresh refused: " + e.message));
};
$("b-kill").onclick = () => {
  post("/api/kill").then((d) => note(d.killed ? "termination sent" : "nothing running"))
    .catch((e) => note("kill failed: " + e.message));
};
$("b-clear").onclick = () => { $("log").replaceChildren(); };

async function poll() {
  try {
    const resp = await fetch("/api/state");
    if (!resp.ok) return;
    renderState(await resp.json());
  } catch (e) { /* server gone: the log pane simply stops updating */ }
}

async function pollLog() {
  try {
    const resp = await fetch("/api/log?since=" + cursor);
    if (!resp.ok) return;
    const data = await resp.json();
    const pre = $("log");
    if (data.dropped) {
      pre.appendChild(document.createTextNode("--- output truncated (buffer overwritten) ---\\n"));
    }
    for (const line of data.lines) {
      pre.appendChild(document.createTextNode(line + "\\n"));
    }
    cursor = data.next;
    text($("logcursor"), "cursor " + cursor);
    if ($("autoscroll").checked) pre.scrollTop = pre.scrollHeight;
  } catch (e) { /* ignore */ }
}

poll();
pollLog();
setInterval(poll, 5000);
setInterval(pollLog, 1000);

// ---- content editor: post.txt + car_photos (POST /api/content*) ----
let postBase = "";
let postDirty = false;

async function uploadFiles(car, files) {
  let done = 0, err = "";
  setNote("carnote", "uploading " + files.length + " image(s) into " + car + " …");
  for (let i = 0; i < files.length; i++) {
    const f = files[i];
    const name = pasteAwareName(f, i);
    const b64 = await blobToB64(f);
    const out = await postJson("/api/content/photo",
                               { car: car, name: name, data_b64: b64 });
    if (out && out.error) err = out.error; else done++;
  }
  await refreshContent(false);
  setNote("carnote", err
    ? "uploaded " + done + " into " + car + " — rejected: " + err
    : "uploaded " + done + " image(s) into " + car + " · now paste or keep editing",
    !!err);
}

/* Clipboard screenshots arrive nameless (or as "image.png") — stamp them so
   repeated pastes never clobber each other; real file names are kept. */
function pasteAwareName(f, i) {
  const n = (f.name || "").trim();
  if (n && n.toLowerCase() !== "image.png" && n.toLowerCase() !== "blob")
    return n;
  const ext = ((f.type || "image/png").split("/")[1] || "png")
    .replace("jpeg", "jpg");
  const ts = new Date().toISOString().replace(/[-:]/g, "").replace(/\\.\\d+Z$/, "");
  return "pasted_" + ts + "_" + i + "." + ext;
}

document.addEventListener("paste", async (e) => {
  const dt = e.clipboardData;
  if (!dt) return;
  const imgs = [];
  for (const f of dt.files || [])
    if ((f.type || "").startsWith("image/")) imgs.push(f);
  if (!imgs.length)
    for (const it of dt.items || [])
      if (it.kind === "file" && (it.type || "").startsWith("image/")) {
        const f = it.getAsFile();
        if (f) imgs.push(f);
      }
  if (!imgs.length) return;          // plain text keeps pasting as text
  e.preventDefault();
  if (!pasteTarget) {
    setNote("carnote", "clipboard has images but no car folder exists — " +
          "create one first, then Ctrl+V", true);
    return;
  }
  await uploadFiles(pasteTarget, imgs);
});

function setNote(id, msg, bad) {
  const n = $(id);
  text(n, msg);
  n.className = "note" + (bad ? " err" : "");
}

async function postJson(url, obj) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(obj),
  });
  return resp.json();
}

function blobToB64(file) {
  return new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => resolve(String(fr.result).split(",")[1]);
    fr.onerror = () => reject(fr.error);
    fr.readAsDataURL(file);
  });
}

let pasteTarget = "";       // folder Ctrl+V lands images in
let carsCache = [];         // last render data, for target switches

function renderCars(cars) {
  carsCache = cars || [];
  const wrap = $("cars");
  wrap.replaceChildren();
  if (cars.length && !cars.some((c) => c.name === pasteTarget))
    pasteTarget = cars[0].name;          // auto-pick (or recover a deleted one)
  for (const car of cars) {
    const box = document.createElement("div");
    box.className = "car";
    if (car.name === pasteTarget) {
      box.classList.add("paste-target");
      box.title = "Ctrl+V pastes clipboard images into this folder";
    }
    box.onclick = () => { pasteTarget = car.name; renderCars(carsCache); };
    const h = document.createElement("h3");
    text(h, car.name + " · " + car.photos.length + " photo(s)");
    if (car.name === pasteTarget) {
      const badge = document.createElement("span");
      badge.className = "ptbadge";
      text(badge, "paste target");
      h.appendChild(badge);
    }
    box.appendChild(h);

    const thumbs = document.createElement("div");
    thumbs.className = "thumbs";
    for (const ph of car.photos) {
      const fig = document.createElement("div");
      fig.className = "thumb";
      const im = document.createElement("img");
      im.src = "/api/content/image?car=" + encodeURIComponent(car.name)
             + "&name=" + encodeURIComponent(ph.name);
      im.alt = ph.name;
      im.title = ph.name + " (" + ph.bytes + " bytes)";
      const del = document.createElement("button");
      del.className = "danger";
      text(del, "×");
      del.title = "delete " + ph.name;
      del.onclick = async () => {
        await postJson("/api/content/photo/delete",
                       { car: car.name, name: ph.name });
        refreshContent(false);
      };
      fig.appendChild(im);
      fig.appendChild(del);
      thumbs.appendChild(fig);
    }
    if (!car.photos.length) {
      const em = document.createElement("div");
      em.className = "note";
      text(em, "no photos in this folder yet");
      thumbs.appendChild(em);
    }
    box.appendChild(thumbs);

    const foot = document.createElement("div");
    foot.className = "carfoot";
    const picker = document.createElement("input");
    picker.type = "file";
    picker.multiple = true;
    picker.accept = "image/*";
    const up = document.createElement("button");
    text(up, "Upload here");
    up.onclick = async () => {
      const files = picker.files;
      if (!files || !files.length) return;
      up.disabled = true;
      await uploadFiles(car.name, Array.from(files));
      up.disabled = false;
    };
    const delcar = document.createElement("button");
    delcar.className = "ghost";
    text(delcar, "Delete folder");
    delcar.onclick = async () => {
      if (!window.confirm("Delete " + car.name + " and ALL its photos?")) return;
      await postJson("/api/content/car/delete", { name: car.name });
      refreshContent(false);
    };
    foot.appendChild(up);
    foot.appendChild(picker);
    foot.appendChild(delcar);
    box.appendChild(foot);
    wrap.appendChild(box);
  }
}

async function refreshContent() {
  const resp = await fetch("/api/content");
  if (!resp.ok) return;
  const st = await resp.json();
  if (!postDirty) {
    $("ptext").value = st.post_text || "";
    postBase = st.post_text || "";
  }
  setNote("ptnote", st.post_exists
    ? "saved · " + (st.post_mtime || "?") + " · " + st.data_dir
    : "no post.txt yet — saving creates it");
  renderCars(st.cars || []);
}

$("ptext").addEventListener("input", () => {
  postDirty = $("ptext").value !== postBase;
});
$("b-save-post").onclick = async () => {
  const out = await postJson("/api/content/post", { text: $("ptext").value });
  if (out && out.error) { setNote("ptnote", out.error, true); return; }
  postDirty = false;
  setNote("ptnote", "saved · " + out.bytes + " bytes — next run posts this");
  refreshContent();
};
$("b-reload-post").onclick = () => { postDirty = false; refreshContent(); };
$("b-addcar").onclick = async () => {
  const name = $("newcar").value.trim();
  if (!name) return;
  const out = await postJson("/api/content/car", { name });
  if (out && out.error) { setNote("ptnote", out.error, true); return; }
  $("newcar").value = "";
  refreshContent();
};
refreshContent();
</script>
</body>
</html>
"""


def render_page() -> str:
    """The dashboard document, served verbatim at GET /."""
    return PAGE_HTML
