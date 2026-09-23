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
    --bg:#0f1115; --panel:#171a21; --panel2:#1d212a; --line:#272c37;
    --fg:#e6e9ef; --dim:#9aa3b2; --ok:#2ecc71; --warn:#f1c40f;
    --bad:#e74c3c; --accent:#4f8cff; --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }
  header { padding:14px 18px; border-bottom:1px solid var(--line);
           background:var(--panel); position:sticky; top:0; z-index:5; }
  h1 { margin:0 0 6px; font-size:16px; letter-spacing:.3px; }
  h2 { margin:0 0 10px; font-size:13px; text-transform:uppercase;
       letter-spacing:.08em; color:var(--dim); }
  .row { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
  .wrap { padding:16px 18px 40px; display:grid; gap:16px;
          grid-template-columns:1fr; max-width:1500px; }
  @media (min-width:1100px) { .cols { display:grid; gap:16px;
          grid-template-columns:1fr 1fr; } }
  .card { background:var(--panel); border:1px solid var(--line);
          border-radius:10px; padding:14px; overflow:hidden; }
  .pill { display:inline-block; padding:2px 8px; border-radius:999px;
          font-size:12px; border:1px solid var(--line); background:var(--panel2);
          color:var(--dim); }
  .pill.ok { color:var(--ok); border-color:#1f6b3f; }
  .pill.warn { color:var(--warn); border-color:#6b5d1f; }
  .pill.bad { color:var(--bad); border-color:#6b2420; }
  .pill.live { color:#fff; background:#8b1e1e; border-color:#c0392b;
               font-weight:600; }
  button { background:var(--panel2); color:var(--fg); border:1px solid var(--line);
           border-radius:7px; padding:7px 12px; font-size:13px; cursor:pointer; }
  button:hover:not(:disabled) { border-color:var(--accent); }
  button:disabled { opacity:.45; cursor:not-allowed; }
  button.danger { background:#3a1a1a; border-color:#6b2420; color:#ffbdb6; }
  button.danger:hover:not(:disabled) { border-color:var(--bad); }
  input[type=text] { background:#0c0e12; color:var(--fg);
        border:1px solid var(--line); border-radius:7px; padding:7px 10px;
        font-family:var(--mono); width:150px; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th,td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line);
          vertical-align:top; }
  th { color:var(--dim); font-weight:600; font-size:11px;
       text-transform:uppercase; letter-spacing:.06em; white-space:nowrap; }
  td.num { text-align:right; font-family:var(--mono); }
  .scroll { max-height:340px; overflow:auto; }
  .mono { font-family:var(--mono); font-size:12px; }
  .dim { color:var(--dim); }
  .empty { color:var(--dim); font-style:italic; padding:8px 0; }
  #log { background:#0b0d11; border:1px solid var(--line); border-radius:8px;
         padding:10px; height:300px; overflow:auto; white-space:pre-wrap;
         word-break:break-word; font-family:var(--mono); font-size:12px;
         margin:0; }
  .stat { display:flex; gap:18px; flex-wrap:wrap; margin-top:8px; }
  .stat div { min-width:96px; }
  .stat b { display:block; font-size:18px; font-family:var(--mono); }
  .stat span { color:var(--dim); font-size:11px; text-transform:uppercase; }
  .note { color:var(--dim); font-size:12px; margin-top:8px; }
  .err { color:var(--bad); }
</style>
</head>
<body>
<header>
  <h1>autoposter · local control <span class="pill" id="ident">…</span>
      <span class="pill" id="mode">…</span></h1>
  <div class="row" id="controls">
    <button id="b-dry-groups">Dry run · groups</button>
    <button id="b-dry-cross">Dry run · crosspost</button>
    <button id="b-refresh-groups">Refresh groups (read-only)</button>
    <button id="b-refresh-listings" disabled title="no listings CLI exists yet">Refresh listings</button>
    <input type="text" id="confirm" placeholder="type PUBLICAR" autocomplete="off"
           spellcheck="false" aria-label="live confirmation phrase">
    <button class="danger" id="b-live-groups">LIVE · groups</button>
    <button class="danger" id="b-live-cross">LIVE · crosspost</button>
    <button class="danger" id="b-kill">Kill run</button>
    <span class="pill" id="runstate">idle</span>
  </div>
  <div class="note" id="hint">Live buttons need the phrase
    <b class="mono">PUBLICAR</b> typed in the box; a live run also needs
    <span class="mono">AP_DRY_RUN=false</span> in .env (the CLI refuses otherwise).</div>
  <div class="note err" id="action"></div>
</header>

<div class="wrap">
  <div class="card">
    <h2>Status</h2>
    <div class="stat" id="stats"></div>
    <div class="note" id="statusnote"></div>
  </div>

  <div class="cols">
    <div class="card">
      <h2>Joined groups &amp; rotation</h2>
      <div class="scroll"><table id="groups">
        <thead><tr><th>next</th><th>group id</th><th>name</th>
          <th>last attempt</th><th>status</th><th>pub.</th><th>att.</th></tr></thead>
        <tbody></tbody></table></div>
      <div class="note" id="groupsnote"></div>
    </div>
    <div class="card">
      <h2>Marketplace listings &amp; crosspost coverage</h2>
      <div class="scroll"><table id="listings">
        <thead><tr><th>listing id</th><th>title</th><th>price</th>
          <th>approved</th><th>rejected</th><th>batches</th>
          <th>groups</th></tr></thead>
        <tbody></tbody></table></div>
      <div class="note" id="listingsnote"></div>
    </div>
  </div>

  <div class="cols">
    <div class="card">
      <h2>Recent runs (ledger)</h2>
      <div class="scroll"><table id="runs">
        <thead><tr><th>run id</th><th>when</th><th>kind</th><th>dry</th>
          <th>pub</th><th>staged</th><th>fail</th><th>skip</th>
          <th>xpost</th></tr></thead>
        <tbody></tbody></table></div>
      <div class="note" id="runsnote"></div>
    </div>
    <div class="card">
      <h2>Run logs on disk</h2>
      <div class="scroll"><table id="logs">
        <thead><tr><th>file</th><th>modified</th><th>bytes</th></tr></thead>
        <tbody></tbody></table></div>
      <div class="note" id="logsnote"></div>
    </div>
  </div>

  <div class="card">
    <h2>Live output <span class="pill" id="logcursor"></span></h2>
    <div class="row" style="margin-bottom:8px">
      <button id="b-clear">Clear view</button>
      <label class="pill"><input type="checkbox" id="autoscroll" checked>
        autoscroll</label>
    </div>
    <pre id="log"></pre>
    <div class="note">Streamed from the running CLI subprocess (stdout+stderr
      merged). Dry runs stage the composer and close it without publishing.</div>
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
    if (row.next) nx.className = "ok";
    td(tr, row.id, "mono");
    td(tr, row.name);
    td(tr, fmtTs(row.last_ts), "mono");
    const cell = td(tr, row.last_status || "never");
    cell.className = "pill " + statusClass(row.last_status);
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
  }, l.available ? "listings snapshot is empty (confirmed 0 active listings)"
                 : "not fetched yet — no listings.json on disk");
  text($("listingsnote"),
    l.available ? ("snapshot " + fmtTs(l.fetched_at) + " · " + l.count + " active listings")
                : "not fetched yet — poster.listings has no CLI yet, so this dashboard cannot refresh it");

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
</script>
</body>
</html>
"""


def render_page() -> str:
    """The dashboard document, served verbatim at GET /."""
    return PAGE_HTML
