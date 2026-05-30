"""Sentinel Sitebuilder — in-app theme-token editor.

A self-contained, owner-gated web page served by SMDL itself (same origin as
the token API, so it reuses the existing cookie / ``X-Init-Data`` auth — no
CORS, no second login). It drives the two endpoints already on the API:

    GET  /api/miniapp/theme-tokens   -> load the live tokens
    POST /api/miniapp/theme-tokens   -> overwrite them (owner-only)

Editing + Save rewrites ``theme_tokens.json`` via ``themes.save_tokens`` and
the running app hot-reloads it, so every surface (APK / Windows / in-Telegram)
restyles with no code change. This is the seed of the cross-pillar Sitebuilder
desktop app — the Tauri window (``desktop/``) simply points at this page.

The page builds its form **generically** from the loaded token shape, so new
palette fields / intensity keys / extra palettes appear automatically. Live
preview replicates ``themes.py``'s var mapping client-side.
"""

from __future__ import annotations

# Editor chrome is deliberately styled with its own fixed dark palette (not the
# theme tokens) so the tool stays legible while you are mid-edit on a palette.
SITEBUILDER_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Sentinel Sitebuilder</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  :root {
    --sb-bg:#0a0b0d; --sb-panel:#131519; --sb-panel-2:#181b20; --sb-line:#262a31;
    --sb-fg:#e8edf3; --sb-muted:#8b94a1; --sb-accent:#2af6ff; --sb-danger:#ff453a;
    --sb-ok:#34c759; --sb-radius:12px;
  }
  * { box-sizing:border-box; }
  html,body { margin:0; height:100%; }
  body {
    background:var(--sb-bg); color:var(--sb-fg);
    font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,system-ui,sans-serif;
    -webkit-font-smoothing:antialiased;
  }
  .topbar {
    display:flex; align-items:center; gap:12px; padding:12px 18px;
    border-bottom:1px solid var(--sb-line); background:var(--sb-panel);
    position:sticky; top:0; z-index:10;
  }
  .topbar h1 { font-size:15px; margin:0; letter-spacing:.04em; text-transform:uppercase; }
  .topbar .grow { flex:1; }
  .status { font-size:12.5px; color:var(--sb-muted); min-height:1em; }
  .status.ok { color:var(--sb-ok); } .status.err { color:var(--sb-danger); }
  .wrap { display:grid; grid-template-columns:1fr 1fr; gap:18px; padding:18px;
          max-width:1200px; margin:0 auto; align-items:start; }
  @media (max-width:820px) { .wrap { grid-template-columns:1fr; } }
  .panel { background:var(--sb-panel); border:1px solid var(--sb-line);
           border-radius:var(--sb-radius); padding:16px; }
  .panel.sticky { position:sticky; top:70px; }
  h2 { font-size:12px; letter-spacing:.08em; text-transform:uppercase;
       color:var(--sb-muted); margin:0 0 12px; }
  .seg { display:flex; gap:6px; flex-wrap:wrap; margin-bottom:14px; }
  .seg button {
    background:var(--sb-panel-2); color:var(--sb-fg); border:1px solid var(--sb-line);
    border-radius:9px; padding:6px 12px; font-size:13px; cursor:pointer;
  }
  .seg button.on { border-color:var(--sb-accent); color:var(--sb-accent);
                   box-shadow:0 0 0 1px var(--sb-accent) inset; }
  .field { display:flex; align-items:center; gap:10px; margin:9px 0; }
  .field label { width:118px; flex:none; color:var(--sb-muted); font-size:12.5px;
                 white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .field input[type=text], .field select {
    flex:1; min-width:0; background:var(--sb-bg); color:var(--sb-fg);
    border:1px solid var(--sb-line); border-radius:8px; padding:6px 9px; font-size:13px;
    font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  }
  .field input[type=color] {
    width:34px; height:30px; padding:0; border:1px solid var(--sb-line);
    border-radius:7px; background:none; cursor:pointer; flex:none;
  }
  .field input:focus, .field select:focus { outline:none; border-color:var(--sb-accent); }
  .row-actions { display:flex; gap:8px; margin-top:14px; flex-wrap:wrap; }
  .btn {
    background:var(--sb-accent); color:#04141a; border:none; border-radius:9px;
    padding:9px 16px; font-size:13.5px; font-weight:600; cursor:pointer;
  }
  .btn.sec { background:var(--sb-panel-2); color:var(--sb-fg); border:1px solid var(--sb-line); }
  .btn.danger { background:transparent; color:var(--sb-danger); border:1px solid var(--sb-danger); }
  .btn:disabled { opacity:.5; cursor:not-allowed; }
  .hint { font-size:12px; color:var(--sb-muted); margin:4px 0 0; }
  /* ── live preview ─────────────────────────────────────────────── */
  #pv-stage {
    border-radius:14px; padding:18px; border:1px solid var(--sb-line);
    background:var(--bg); color:var(--fg); overflow:hidden;
  }
  #pv-stage * { box-sizing:border-box; }
  .pv-card {
    background:var(--surface); border:1px solid var(--separator);
    border-radius:var(--radius); padding:14px; margin-bottom:12px; box-shadow:var(--glow);
  }
  .pv-card h3 { margin:0 0 4px; font-size:15px; color:var(--fg); }
  .pv-card p { margin:0; font-size:13px; color:var(--muted); }
  .pv-tiles { display:grid; grid-template-columns:repeat(2,1fr); gap:10px; margin-bottom:12px; }
  .pv-tile {
    position:relative; background:var(--surface); border:1px solid var(--separator);
    border-radius:var(--tile-radius); padding:14px 12px; box-shadow:var(--glow);
    overflow:hidden;
  }
  .pv-tile::before {
    content:""; position:absolute; inset:0; pointer-events:none;
    background:linear-gradient(180deg, rgba(255,255,255,calc(0.06*var(--sheen))), transparent 60%);
  }
  .pv-tile .ic { font-size:20px; }
  .pv-tile .lb {
    display:block; margin-top:6px; font-size:12px; color:var(--fg);
    text-transform:var(--label-tt); letter-spacing:var(--label-ls);
  }
  .pv-btn-row { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:12px; }
  .pv-btn {
    border:none; border-radius:var(--radius); padding:9px 15px; font-size:13px;
    font-weight:600; cursor:default;
    background:linear-gradient(var(--metal-angle), var(--accent) 0%, var(--accent-2) 100%);
    color:var(--button-text); box-shadow:var(--glow);
  }
  .pv-btn.sec { background:var(--surface); color:var(--accent);
                border:1px solid var(--accent-line); }
  .pv-btn.danger { background:transparent; color:var(--destructive);
                   border:1px solid var(--destructive); }
  .pv-link { color:var(--link); text-decoration:none; font-size:13px; }
  .pv-input {
    width:100%; background:var(--bg-elev); color:var(--fg);
    border:1px solid var(--separator); border-radius:var(--radius);
    padding:8px 10px; font-size:13px; margin-bottom:12px;
  }
  .pv-chips { display:flex; gap:8px; flex-wrap:wrap; }
  .pv-chip { font-size:11px; padding:3px 9px; border-radius:999px;
             background:var(--accent-soft); color:var(--accent); }
  .gate { padding:40px 22px; text-align:center; color:var(--sb-muted); }
  .gate b { color:var(--sb-fg); }
</style>
</head>
<body>
<div class="topbar">
  <a class="btn sec" href="/app" style="text-decoration:none;flex:none">← Sentinel Media</a>
  <h1>⬡ Sentinel Sitebuilder</h1>
  <div class="grow"></div>
  <div id="status" class="status"></div>
</div>

<div id="gate" class="gate">Loading tokens…</div>

<div id="app" class="wrap" style="display:none">
  <!-- editor -->
  <div>
    <div class="panel">
      <h2>Palette</h2>
      <div id="palette-seg" class="seg"></div>
      <div id="palette-fields"></div>
    </div>

    <div class="panel" style="margin-top:18px">
      <h2>Intensity</h2>
      <div id="intensity-seg" class="seg"></div>
      <div id="intensity-fields"></div>
    </div>

    <div class="panel" style="margin-top:18px">
      <h2>Constants &amp; defaults</h2>
      <div id="const-fields"></div>
      <div id="default-fields"></div>
    </div>

    <div class="row-actions">
      <button id="save-btn" class="btn">Save tokens</button>
      <button id="reset-btn" class="btn sec">Reset to saved</button>
    </div>
    <p class="hint">Save rewrites <code>theme_tokens.json</code>. Every surface
       (APK · Windows · Telegram) restyles immediately — no code change, no restart.</p>
  </div>

  <!-- live preview -->
  <div class="panel sticky">
    <h2>Live preview</h2>
    <div class="seg">
      <span style="color:var(--sb-muted);font-size:12px;align-self:center">Theme</span>
      <div id="pv-theme-seg" style="display:flex;gap:6px;flex-wrap:wrap"></div>
    </div>
    <div class="seg">
      <span style="color:var(--sb-muted);font-size:12px;align-self:center">FX</span>
      <div id="pv-fx-seg" style="display:flex;gap:6px;flex-wrap:wrap"></div>
    </div>
    <div id="pv-stage">
      <div class="pv-tiles">
        <div class="pv-tile"><span class="ic">🎬</span><span class="lb">Theater</span></div>
        <div class="pv-tile"><span class="ic">🎵</span><span class="lb">Music</span></div>
        <div class="pv-tile"><span class="ic">⬇</span><span class="lb">Downloads</span></div>
        <div class="pv-tile"><span class="ic">⚙</span><span class="lb">Settings</span></div>
      </div>
      <div class="pv-card">
        <h3>Now playing</h3>
        <p>Metallic surfaces, accent glow, and intensity in one glance.</p>
      </div>
      <input class="pv-input" value="Search the library…" readonly>
      <div class="pv-btn-row">
        <button class="pv-btn">Primary</button>
        <button class="pv-btn sec">Secondary</button>
        <button class="pv-btn danger">Delete</button>
      </div>
      <div class="pv-chips">
        <span class="pv-chip">4K</span><span class="pv-chip">HDR</span>
        <span class="pv-chip">Atmos</span>
        <a class="pv-link" href="#" onclick="return false">View all →</a>
      </div>
    </div>
  </div>
</div>

<script>
"use strict";
const tg = window.Telegram?.WebApp;
try { tg?.ready(); tg?.expand?.(); } catch (e) {}
const initData = tg?.initData || '';

function api(path, opts = {}) {
  return fetch(path, {
    ...opts,
    headers: { 'X-Init-Data': initData, 'Content-Type': 'application/json', ...(opts.headers||{}) },
  }).then(async r => {
    const txt = await r.text();
    if (r.ok) { try { return JSON.parse(txt); } catch { return {}; } }
    let msg = 'HTTP ' + r.status;
    try { const j = JSON.parse(txt); msg = j.detail || j.error || msg; } catch {}
    const err = new Error(msg); err.status = r.status; throw err;
  });
}

const $ = id => document.getElementById(id);
function setStatus(msg, kind) { const el = $('status'); el.textContent = msg||''; el.className = 'status' + (kind?(' '+kind):''); }
const isHex = v => typeof v === 'string' && /^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.test(v.trim());

// Derived vars — mirror of themes.py _DERIVED (must stay in sync).
const DERIVED = {
  'link':'var(--accent)', 'button':'var(--accent)',
  'section':'var(--surface-1)', 'card':'var(--surface-1)',
  'surface':'linear-gradient(var(--metal-angle), var(--surface-2) 0%, var(--surface-1) 100%)',
  'accent-soft':'rgba(var(--accent-rgb), 0.12)', 'accent-line':'rgba(var(--accent-rgb), 0.55)',
};

let TOKENS = null;        // working copy (mutated by the form)
let curPalette = null, curIntensity = null;
let pvTheme = null, pvFx = null;

function hexToRgb(hex) {
  let h = hex.trim().replace('#','');
  if (h.length === 3) h = h.split('').map(c=>c+c).join('');
  const n = parseInt(h,16);
  return ((n>>16)&255) + ', ' + ((n>>8)&255) + ', ' + (n&255);
}

// ── form rendering (generic over token shape) ──────────────────────────────
function fieldRow(labelTxt, value, onChange) {
  const wrap = document.createElement('div'); wrap.className = 'field';
  const lab = document.createElement('label'); lab.textContent = labelTxt; lab.title = labelTxt;
  wrap.appendChild(lab);
  const txt = document.createElement('input'); txt.type = 'text'; txt.value = value;
  if (isHex(value)) {
    const col = document.createElement('input'); col.type = 'color'; col.value = value;
    col.oninput = () => { txt.value = col.value; onChange(col.value); };
    wrap.appendChild(col);
    txt.oninput = () => { if (isHex(txt.value)) col.value = txt.value.trim(); onChange(txt.value); };
  } else {
    txt.oninput = () => onChange(txt.value);
  }
  wrap.appendChild(txt);
  return wrap;
}

function selectRow(labelTxt, value, options, onChange) {
  const wrap = document.createElement('div'); wrap.className = 'field';
  const lab = document.createElement('label'); lab.textContent = labelTxt; wrap.appendChild(lab);
  const sel = document.createElement('select');
  options.forEach(o => { const op=document.createElement('option'); op.value=o; op.textContent=o; if(o===value)op.selected=true; sel.appendChild(op); });
  sel.onchange = () => onChange(sel.value);
  wrap.appendChild(sel); return wrap;
}

function renderPaletteSeg() {
  const seg = $('palette-seg'); seg.innerHTML = '';
  Object.keys(TOKENS.palettes).forEach(pid => {
    const b = document.createElement('button');
    b.textContent = TOKENS.palettes[pid].name || pid;
    if (pid === curPalette) b.className = 'on';
    b.onclick = () => { curPalette = pid; pvTheme = pid; renderAll(); };
    seg.appendChild(b);
  });
}

function renderPaletteFields() {
  const box = $('palette-fields'); box.innerHTML = '';
  const p = TOKENS.palettes[curPalette];
  Object.keys(p).forEach(k => {
    box.appendChild(fieldRow(k, p[k], v => {
      p[k] = v;
      // accent drives accent-rgb so the soft/line derivations track the picker.
      if (k === 'accent' && isHex(v) && 'accent-rgb' in p) {
        p['accent-rgb'] = hexToRgb(v);
        renderPaletteFields();
      }
      if (k === 'name') renderPaletteSeg();
      applyPreview();
    }));
  });
}

function renderIntensitySeg() {
  const seg = $('intensity-seg'); seg.innerHTML = '';
  Object.keys(TOKENS.intensities).forEach(iid => {
    const b = document.createElement('button'); b.textContent = iid;
    if (iid === curIntensity) b.className = 'on';
    b.onclick = () => { curIntensity = iid; pvFx = iid; renderAll(); };
    seg.appendChild(b);
  });
}

function renderIntensityFields() {
  const box = $('intensity-fields'); box.innerHTML = '';
  const it = TOKENS.intensities[curIntensity];
  Object.keys(it).forEach(k => {
    if (k === 'label-tt') {
      box.appendChild(selectRow(k, it[k], ['none','uppercase','lowercase','capitalize'], v=>{it[k]=v; applyPreview();}));
    } else {
      box.appendChild(fieldRow(k, it[k], v=>{it[k]=v; applyPreview();}));
    }
  });
}

function renderConstFields() {
  const box = $('const-fields'); box.innerHTML = '';
  const c = TOKENS.constants || (TOKENS.constants = {});
  Object.keys(c).forEach(k => box.appendChild(fieldRow(k, c[k], v=>{c[k]=v; applyPreview();})));
}

function renderDefaultFields() {
  const box = $('default-fields'); box.innerHTML = '';
  const d = TOKENS.defaults || (TOKENS.defaults = {});
  box.appendChild(selectRow('default theme', d.theme, Object.keys(TOKENS.palettes), v=>{d.theme=v;}));
  box.appendChild(selectRow('default fx', d.fx, Object.keys(TOKENS.intensities), v=>{d.fx=v;}));
}

function renderPvSegs() {
  const ts = $('pv-theme-seg'); ts.innerHTML = '';
  Object.keys(TOKENS.palettes).forEach(pid => {
    const b = document.createElement('button');
    b.className = 'seg' ; b.textContent = pid;
    b.style.cssText = 'background:var(--sb-panel-2);color:var(--sb-fg);border:1px solid var(--sb-line);border-radius:8px;padding:5px 10px;font-size:12px;cursor:pointer';
    if (pid === pvTheme) { b.style.borderColor='var(--sb-accent)'; b.style.color='var(--sb-accent)'; }
    b.onclick = () => { pvTheme = pid; renderPvSegs(); applyPreview(); };
    ts.appendChild(b);
  });
  const fs = $('pv-fx-seg'); fs.innerHTML = '';
  Object.keys(TOKENS.intensities).forEach(iid => {
    const b = document.createElement('button'); b.textContent = iid;
    b.style.cssText = 'background:var(--sb-panel-2);color:var(--sb-fg);border:1px solid var(--sb-line);border-radius:8px;padding:5px 10px;font-size:12px;cursor:pointer';
    if (iid === pvFx) { b.style.borderColor='var(--sb-accent)'; b.style.color='var(--sb-accent)'; }
    b.onclick = () => { pvFx = iid; renderPvSegs(); applyPreview(); };
    fs.appendChild(b);
  });
}

// Build the CSS-var set for the preview stage — mirrors render_theme_css():
// palette pairs + intensity pairs + constants + derived.
function applyPreview() {
  const stage = $('pv-stage');
  const p = TOKENS.palettes[pvTheme] || {};
  const it = TOKENS.intensities[pvFx] || {};
  const c = TOKENS.constants || {};
  stage.style.cssText = ''; // clear inline vars
  Object.entries(p).forEach(([k,v]) => { if (k!=='name') stage.style.setProperty('--'+k, v); });
  Object.entries(it).forEach(([k,v]) => stage.style.setProperty('--'+k, v));
  Object.entries(c).forEach(([k,v]) => stage.style.setProperty('--'+k, v));
  Object.entries(DERIVED).forEach(([k,v]) => stage.style.setProperty('--'+k, v));
}

function renderAll() {
  renderPaletteSeg(); renderPaletteFields();
  renderIntensitySeg(); renderIntensityFields();
  renderConstFields(); renderDefaultFields();
  renderPvSegs(); applyPreview();
}

async function load() {
  try {
    TOKENS = await api('/api/miniapp/theme-tokens');
  } catch (e) {
    $('gate').innerHTML = e.status === 401 || e.status === 403
      ? '<b>Owner only.</b><br>Open the Sitebuilder from the Sentinel app where you are signed in.'
      : '<b>Couldn\'t load tokens.</b><br>' + e.message;
    return;
  }
  curPalette   = (TOKENS.defaults && TOKENS.defaults.theme) || Object.keys(TOKENS.palettes)[0];
  curIntensity = (TOKENS.defaults && TOKENS.defaults.fx)    || Object.keys(TOKENS.intensities)[0];
  pvTheme = curPalette; pvFx = curIntensity;
  $('gate').style.display = 'none';
  $('app').style.display = 'grid';
  renderAll();
  setStatus('Loaded.');
}

$('save-btn').onclick = async () => {
  const btn = $('save-btn'); btn.disabled = true; setStatus('Saving…');
  try {
    const res = await api('/api/miniapp/theme-tokens', { method:'POST', body: JSON.stringify(TOKENS) });
    TOKENS = res.tokens || TOKENS;
    setStatus('Saved — every surface restyled.', 'ok');
    renderAll();
  } catch (e) {
    setStatus('Save failed: ' + e.message, 'err');
  } finally { btn.disabled = false; }
};

$('reset-btn').onclick = () => { setStatus('Reloading saved tokens…'); load(); };

load();
</script>
</body>
</html>
"""
