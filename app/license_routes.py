"""License-key surfaces: owner admin API + page, and the public validate
endpoint the APKs call. Crypto/format/persistence live in licensing.py and
database.py; this module is the HTTP layer.

Owner routes reuse the Mini App's auth guards (_verify + _require_owner) and
the `smdl.license` scope, so a future scoped delegate could be granted license
admin without full owner rights. The validate endpoint is intentionally public
— the key itself is the credential.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from . import database as db
from . import edition
from . import entitlements
from . import licensing
from . import license_registry
from . import play_billing
from . import profile
from .miniapp import _require_owner, _verify, require_scope

router = APIRouter()


def _accepted_tiers() -> set[str]:
    """Which license tiers THIS deployment honours at validate time. Defaults
    from the edition flag — private serves Family keys, community serves
    Community keys — so a key minted for one edition can't activate the other.
    Override with LICENSE_ACCEPTED_TIERS=community,family to honour both (e.g.
    an operator box that also runs community clients)."""
    raw = (os.environ.get("LICENSE_ACCEPTED_TIERS") or "").strip()
    if raw:
        return {t.strip().lower() for t in raw.split(",") if t.strip()}
    return {licensing.TIER_FAMILY} if edition.is_private() else {licensing.TIER_COMMUNITY}


# ── Owner admin API ──────────────────────────────────────────────────────────


@router.post("/api/miniapp/admin/license/create")
async def license_create(request: Request):
    """Mint a new key (owner-only). The plaintext key_code is returned ONCE —
    it's never stored, only an HMAC of its secret is."""
    p = await _verify(request)
    _require_owner(p)
    require_scope(p, "smdl.license")
    if not licensing.is_configured():
        raise HTTPException(503, "licensing not configured (set LICENSE_SIGNING_SECRET)")
    body = await request.json()
    try:
        tier = licensing.normalise_tier(body.get("tier", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    issued_to = (body.get("issued_to") or "").strip() or None
    note = (body.get("note") or "").strip() or None
    try:
        seats = licensing.clamp_seats(int(body.get("seats", 1)))
        valid_days = int(body.get("valid_days", 365))
    except (TypeError, ValueError):
        raise HTTPException(400, "seats and valid_days must be integers")

    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    expires_at = licensing.expiry_from_days(valid_days)
    key_id, secret, key_code = licensing.generate_key(tier)
    await db.license_create(
        key_id=key_id, tier=tier, secret_hash=licensing.sign_secret(secret),
        issued_to=issued_to, seats=seats, issued_at=now_iso,
        expires_at=expires_at, note=note,
    )
    # Best-effort mirror to the central registry (never blocks issuance).
    license_registry.fire_upsert({
        "key_id": key_id, "tier": tier, "status": "active",
        "expires_at": expires_at, "issued_to": issued_to, "note": note,
    })
    return {
        "ok": True,
        "key_code": key_code,   # show once
        "key_id": key_id,
        "tier": tier,
        "seats": seats,
        "expires_at": expires_at,
        "issued_to": issued_to,
    }


def _decorate(row: dict) -> dict:
    """Add a derived display status (active / revoked / expired)."""
    status = row.get("status", "active")
    if status == "active" and licensing.is_expired(row.get("expires_at", "")):
        status = "expired"
    out = dict(row)
    out.pop("secret_hash", None)  # never expose, even the hash
    out["display_status"] = status
    return out


@router.get("/api/miniapp/admin/license/list")
async def license_list(request: Request):
    p = await _verify(request)
    _require_owner(p)
    require_scope(p, "smdl.license")
    rows = await db.license_list()
    return {
        "ok": True,
        "configured": licensing.is_configured(),
        "keys": [_decorate(r) for r in rows],
        "tiers": list(licensing.TIERS),
        "grace_seconds": licensing.GRACE_SECONDS,
        "registry": license_registry.status(),
    }


@router.post("/api/miniapp/admin/license/revoke")
async def license_revoke(request: Request):
    p = await _verify(request)
    _require_owner(p)
    require_scope(p, "smdl.license")
    body = await request.json()
    key_id = (body.get("key_id") or "").strip()
    if not key_id:
        raise HTTPException(400, "key_id required")
    changed = await db.license_revoke(key_id)
    # Mirror the revocation centrally (best-effort). Pull the row so the
    # registry gets accurate metadata even if it never saw the key before.
    row = await db.license_get(key_id)
    if row:
        license_registry.fire_upsert(dict(row), force_revoked=True)
    return {"ok": True, "revoked": changed}


@router.post("/api/miniapp/admin/license/sync")
async def license_sync(request: Request):
    """Reconcile every local key to the central registry. Idempotent; safe to
    re-run. Owner-only."""
    p = await _verify(request)
    _require_owner(p)
    require_scope(p, "smdl.license")
    rows = [dict(r) for r in await db.license_list()]
    result = await license_registry.backfill(rows)
    return {"ok": result.get("ok", False), **result}


@router.get("/api/miniapp/admin/license/{key_id}/activations")
async def license_activations(key_id: str, request: Request):
    p = await _verify(request)
    _require_owner(p)
    require_scope(p, "smdl.license")
    if not await db.license_get(key_id):
        raise HTTPException(404, "key not found")
    return {"ok": True, "activations": await db.license_list_activations(key_id)}


# ── Public validation (APK-facing) ───────────────────────────────────────────


@router.post("/api/license/validate")
async def license_validate(request: Request):
    """Online check the APK calls on activation + periodically. Returns a grant
    (which the APK caches for GRACE_SECONDS) or {valid:false, reason}. Always
    HTTP 200 so the client can branch on `valid` without exception handling.

    device_id is optional: with it, the seat limit is enforced and the device
    is recorded; without it this is a stateless "is this key good" check."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    key = (body.get("key") or "").strip()
    device_id = (body.get("device_id") or "").strip() or None
    device_label = (body.get("device_label") or "").strip() or None

    # A play-profile deployment serves the Play Store TWA, where Google Play
    # Billing is the only permitted paid rail. Refuse external license keys here
    # so there's no off-Play unlock path (the teeth behind allow_key_redeem).
    if not profile.allow_key_redeem():
        return {"valid": False, "reason": "play_build_uses_billing"}
    if not licensing.is_configured():
        return {"valid": False, "reason": "not_configured"}
    parsed = licensing.parse_key_code(key)
    if not parsed:
        return {"valid": False, "reason": "malformed"}
    _tier, key_id, secret = parsed
    row = await db.license_get(key_id)
    if not row:
        return {"valid": False, "reason": "not_found"}
    if not licensing.verify_secret(secret, row["secret_hash"]):
        return {"valid": False, "reason": "bad_secret"}
    if row.get("status") != "active":
        return {"valid": False, "reason": "revoked"}
    if licensing.is_expired(row["expires_at"]):
        return {"valid": False, "reason": "expired"}
    # Edition gate: a real, active key still can't activate the wrong edition
    # (a Community key on a private deployment, or vice versa). Checked against
    # the persisted row tier, not the code prefix, so a forged prefix can't slip
    # an unauthorised tier past the secret check.
    if row["tier"] not in _accepted_tiers():
        return {"valid": False, "reason": "wrong_edition"}

    if device_id:
        if not await db.license_activation_exists(key_id, device_id):
            if await db.license_count_activations(key_id) >= int(row["seats"]):
                return {"valid": False, "reason": "seat_limit"}
        await db.license_record_activation(key_id, device_id, device_label)

    grant = licensing.build_grant(row)
    grant.update(entitlements.enrich(row))
    return licensing.sign_grant(grant)


# ── Play Billing rail (parallel to license keys, same entitlement SoT) ────────


@router.post("/api/billing/play/verify")
async def play_billing_verify(request: Request):
    """Verify a Google Play purchase/subscription token and return a grant.

    The Play build's only paid rail. Returns the same grant shape as
    /api/license/validate (valid, plan, entitlements, limits) so the client
    treats both rails identically. Always HTTP 200 — branch on `valid`.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    grant = await play_billing.verify(
        purchase_token=(body.get("purchase_token") or ""),
        product_id=(body.get("product_id") or ""),
        kind=(body.get("kind") or "product"),
    )
    # Sign valid grants so the cached copy is tamper-evident — same path as the
    # license rail. Fail closed if signing isn't configured on this deployment
    # rather than emitting a forgeable unsigned grant.
    if grant.get("valid"):
        if not licensing.is_configured():
            return {"valid": False, "reason": "signing_not_configured"}
        grant = licensing.sign_grant(grant)
    return grant


# ── Owner admin page ─────────────────────────────────────────────────────────


@router.get("/app/licenses", response_class=HTMLResponse)
async def license_admin_page():
    """Owner license console. The page itself is harmless HTML; every API call
    it makes is owner-gated server-side."""
    return HTMLResponse(_LICENSE_HTML)


@router.get("/app/entitlements", response_class=HTMLResponse)
async def entitlements_matrix_page(request: Request):
    """Features × plans matrix — what each license tier unlocks on each surface.
    Non-sensitive (it's the published plan structure), so anyone can view it as
    a pricing/features reference; the caller's CURRENT (or previewed) plan is
    highlighted. Pairs with `entitlements.feature_banner()` for the in-place
    'locked' message on live deployments."""
    from . import grant_transport, edition as _edition
    grant = await grant_transport.resolve_grant(request)
    plan = grant.get("plan", "free")
    matrix = entitlements.render_matrix(current_plan=plan)
    ed = "Community build (safe-by-default)" if _edition.is_community() else "Private build (owner)"
    enf = "enforced" if grant_transport.enforcement_active() else "not enforced (owner box)"
    plan_label = entitlements.PLAN_LABEL.get(plan, plan.title())
    return HTMLResponse(
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width, initial-scale=1'>"
        "<title>Plans & Features — Sentinel Media</title><style>"
        ":root{color-scheme:dark}*{box-sizing:border-box}"
        "body{margin:0;background:#0d1014;color:#e6e9ef;"
        "font:14px/1.5 Inter,system-ui,-apple-system,sans-serif}"
        ".wrap{max-width:880px;margin:0 auto;padding:18px 16px 60px}"
        "h1{font-size:20px;margin:6px 0 2px}.sub{color:#8b94a3;margin:0 0 14px;font-size:13px}"
        "a.back{color:#6ea0ff;text-decoration:none;font-size:13px}"
        ".card{background:#15191f;border:1px solid #2a313c;border-radius:12px;padding:16px;margin-bottom:16px;overflow-x:auto}"
        ".meta{font-size:12px;color:#8b94a3;margin:2px 0 12px}"
        ".chip{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11px;font-weight:600;background:#1b2a3a;color:#7fb8ff;margin-right:6px}"
        "</style></head><body><div class=wrap>"
        "<a class=back href='/app'>&larr; Sentinel Media</a>"
        "<h1>Plans &amp; Features</h1>"
        f"<div class=sub>What each license tier unlocks on each surface. "
        f"You are viewing as <b style='color:#4cd964'>{plan_label}</b>.</div>"
        f"<div class=meta><span class=chip>{ed}</span>"
        f"<span class=chip>Entitlements {enf}</span></div>"
        f"<div class=card>{matrix}</div>"
        "<div class=meta>Plans are cumulative — each tier includes everything "
        "below it. <b>Community</b> also hides some surfaces entirely "
        "(source-admission boundary, ADR&nbsp;MED-001) regardless of plan.</div>"
        "</div></body></html>")


_LICENSE_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Licenses — Sentinel Media</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; background: #0d1014; color: #e6e9ef;
         font: 14px/1.5 Inter, system-ui, -apple-system, sans-serif; }
  .wrap { max-width: 880px; margin: 0 auto; padding: 18px 16px 60px; }
  h1 { font-size: 20px; margin: 6px 0 2px; }
  .sub { color: #8b94a3; margin: 0 0 18px; font-size: 13px; }
  a.back { color: #6ea0ff; text-decoration: none; font-size: 13px; }
  .card { background: #15191f; border: 1px solid #2a313c; border-radius: 12px;
          padding: 16px; margin-bottom: 16px; }
  .card h2 { font-size: 15px; margin: 0 0 12px; }
  label { display: block; font-size: 12px; color: #aab2bf; margin: 10px 0 4px; }
  input, select, textarea {
    width: 100%; padding: 9px 11px; border-radius: 9px; border: 1px solid #2a313c;
    background: #0d1014; color: #e6e9ef; font: inherit; }
  .row { display: flex; gap: 12px; flex-wrap: wrap; }
  .row > div { flex: 1; min-width: 130px; }
  button { padding: 9px 16px; border: 0; border-radius: 9px; font-weight: 600;
           cursor: pointer; font: inherit; }
  .primary { background: #3a6df0; color: #fff; }
  .ghost { background: #232a33; color: #cfd6e0; }
  .danger { background: #3a1f23; color: #ff8e9b; border: 1px solid #5a2a30; }
  .keybox { margin-top: 14px; padding: 14px; border-radius: 10px;
            background: #102016; border: 1px solid #1f4a2e; display: none; }
  .keybox.show { display: block; }
  .keycode { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
             font-size: 13px; word-break: break-all; background: #0d1014;
             padding: 10px; border-radius: 8px; border: 1px solid #2a313c;
             margin: 8px 0; }
  .warn { color: #ffce7a; font-size: 12px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 8px 6px; border-bottom: 1px solid #222932; }
  th { color: #8b94a3; font-weight: 600; font-size: 12px; }
  .pill { display: inline-block; padding: 2px 8px; border-radius: 999px;
          font-size: 11px; font-weight: 600; }
  .pill.active { background: #12331f; color: #6fe39a; }
  .pill.revoked { background: #3a1f23; color: #ff8e9b; }
  .pill.expired { background: #332a12; color: #ffce7a; }
  .pill.community { background: #1b2a3a; color: #7fb8ff; }
  .pill.family { background: #2a1b3a; color: #c79bff; }
  .muted { color: #6b7280; }
  #toast { position: fixed; left: 50%; bottom: 22px; transform: translateX(-50%);
           background: #232a33; color: #e6e9ef; padding: 10px 16px; border-radius: 10px;
           opacity: 0; transition: opacity .2s; pointer-events: none; font-size: 13px; }
  #toast.show { opacity: 1; }
</style>
</head>
<body>
<div class="wrap">
  <a class="back" href="/app">&larr; Back to app</a>
  <h1>License keys</h1>
  <p class="sub">Issue and manage keys for the Community and Family APKs. Each key is time-limited and seat-capped; validation happens online with an offline grace window.</p>

  <div class="card" id="config-warn" style="display:none">
    <h2 style="color:#ffce7a">Signing secret not set</h2>
    <p class="sub">Set <code>LICENSE_SIGNING_SECRET</code> (or <code>OWNER_AUTH_TOKEN</code>) on this instance before issuing keys.</p>
  </div>

  <div class="card">
    <h2>Issue a new key</h2>
    <div class="row">
      <div>
        <label>Edition</label>
        <select id="f-tier">
          <option value="family">Family (full)</option>
          <option value="community">Community</option>
        </select>
      </div>
      <div>
        <label>Seats (devices)</label>
        <input id="f-seats" type="number" min="1" max="50" value="1">
      </div>
      <div>
        <label>Valid for (days)</label>
        <input id="f-days" type="number" min="1" max="3650" value="365">
      </div>
    </div>
    <label>Issued to (name / note)</label>
    <input id="f-to" type="text" placeholder="e.g. Mum's tablet, beta tester Ali">
    <div style="margin-top:14px">
      <button class="primary" id="btn-create">Create key</button>
    </div>
    <div class="keybox" id="keybox">
      <div>New key — <span class="warn">copy it now, it won't be shown again</span></div>
      <div class="keycode" id="keycode"></div>
      <button class="ghost" id="btn-copy">Copy</button>
    </div>
  </div>

  <div class="card">
    <div style="display:flex; align-items:center; justify-content:space-between; gap:10px">
      <h2 style="margin:0">Existing keys</h2>
      <button class="ghost" id="btn-sync" style="display:none" title="Push all keys to the central registry">Sync to registry</button>
    </div>
    <p class="sub" id="registry-line" style="margin:8px 0 0"></p>
    <div id="keys-host" style="margin-top:10px"><p class="muted">Loading…</p></div>
  </div>
</div>
<div id="toast"></div>

<script>
const tg = window.Telegram && window.Telegram.WebApp;
if (tg) { try { tg.ready(); tg.expand(); } catch (e) {} }
const initData = (tg && tg.initData) || '';

async function api(path, opts) {
  opts = opts || {};
  opts.headers = Object.assign({}, opts.headers || {},
    { 'X-Init-Data': initData },
    opts.body ? { 'Content-Type': 'application/json' } : {});
  const r = await fetch(path, opts);
  const text = await r.text();
  let data = {};
  try { data = JSON.parse(text); } catch (e) {}
  if (!r.ok) throw new Error(data.detail || (r.status + ': ' + text.slice(0, 120)));
  return data;
}

function toast(msg, ms) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), ms || 2200);
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function fmtDate(iso) {
  if (!iso) return '—';
  try { return new Date(iso).toISOString().slice(0, 10); } catch (e) { return iso.slice(0, 10); }
}

async function loadKeys() {
  const host = document.getElementById('keys-host');
  try {
    const res = await api('/api/miniapp/admin/license/list');
    document.getElementById('config-warn').style.display = res.configured ? 'none' : 'block';
    const reg = res.registry || {};
    const regLine = document.getElementById('registry-line');
    const syncBtn = document.getElementById('btn-sync');
    if (reg.enabled) {
      regLine.innerHTML = 'Central registry: <span style="color:#6fe39a">connected</span> <span class="muted">(' + esc(reg.url) + ')</span>';
      syncBtn.style.display = '';
    } else {
      regLine.innerHTML = 'Central registry: <span class="muted">not configured — keys are local-only</span>';
      syncBtn.style.display = 'none';
    }
    const keys = res.keys || [];
    if (!keys.length) { host.innerHTML = '<p class="muted">No keys issued yet.</p>'; return; }
    let html = '<table><thead><tr>' +
      '<th>Edition</th><th>Issued to</th><th>Status</th><th>Seats</th>' +
      '<th>Expires</th><th></th></tr></thead><tbody>';
    for (const k of keys) {
      const tier = esc(k.tier);
      const st = esc(k.display_status);
      const seats = (k.activations || 0) + ' / ' + (k.seats || 1);
      const canRevoke = k.status === 'active';
      html += '<tr>' +
        '<td><span class="pill ' + tier + '">' + tier + '</span></td>' +
        '<td>' + (esc(k.issued_to) || '<span class="muted">—</span>') + '</td>' +
        '<td><span class="pill ' + st + '">' + st + '</span></td>' +
        '<td>' + seats + '</td>' +
        '<td>' + fmtDate(k.expires_at) + '</td>' +
        '<td>' + (canRevoke
          ? '<button class="danger" data-revoke="' + esc(k.key_id) + '">Revoke</button>'
          : '<span class="muted">' + esc(k.key_id) + '</span>') +
        '</td></tr>';
    }
    html += '</tbody></table>';
    host.innerHTML = html;
    host.querySelectorAll('[data-revoke]').forEach(btn => {
      btn.addEventListener('click', () => revokeKey(btn.getAttribute('data-revoke')));
    });
  } catch (e) {
    host.innerHTML = '<p class="muted">Could not load keys: ' + esc(e.message) + '</p>';
  }
}

async function createKey() {
  const btn = document.getElementById('btn-create');
  btn.disabled = true;
  try {
    const res = await api('/api/miniapp/admin/license/create', {
      method: 'POST',
      body: JSON.stringify({
        tier: document.getElementById('f-tier').value,
        seats: parseInt(document.getElementById('f-seats').value, 10) || 1,
        valid_days: parseInt(document.getElementById('f-days').value, 10) || 365,
        issued_to: document.getElementById('f-to').value,
      }),
    });
    document.getElementById('keycode').textContent = res.key_code;
    document.getElementById('keybox').classList.add('show');
    document.getElementById('f-to').value = '';
    toast('Key created');
    loadKeys();
  } catch (e) {
    toast('Failed: ' + e.message, 3500);
  } finally {
    btn.disabled = false;
  }
}

async function revokeKey(keyId) {
  if (!confirm('Revoke this key? Devices stop working within the grace window.')) return;
  try {
    await api('/api/miniapp/admin/license/revoke', {
      method: 'POST', body: JSON.stringify({ key_id: keyId }),
    });
    toast('Revoked');
    loadKeys();
  } catch (e) {
    toast('Failed: ' + e.message, 3500);
  }
}

async function syncRegistry() {
  const btn = document.getElementById('btn-sync');
  btn.disabled = true;
  try {
    const res = await api('/api/miniapp/admin/license/sync', { method: 'POST', body: '{}' });
    toast('Synced ' + (res.synced || 0) + '/' + (res.total || 0) + (res.failed ? ' (' + res.failed + ' failed)' : ''));
    loadKeys();
  } catch (e) {
    toast('Sync failed: ' + e.message, 3500);
  } finally {
    btn.disabled = false;
  }
}

document.getElementById('btn-sync').addEventListener('click', syncRegistry);
document.getElementById('btn-create').addEventListener('click', createKey);
document.getElementById('btn-copy').addEventListener('click', () => {
  const code = document.getElementById('keycode').textContent;
  navigator.clipboard.writeText(code).then(() => toast('Copied')).catch(() => toast('Copy failed'));
});
loadKeys();
</script>
</body>
</html>
"""
