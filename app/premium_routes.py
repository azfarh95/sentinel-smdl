"""Premium-manifest + beta-key admin + redemption HTTP layer.

Three groups of routes:

  Owner CRUD          /api/admin/premium/*           — manage premium_users
                      /api/admin/beta_keys/*         — mint/list/revoke keys

  User self-service   POST /api/auth/redeem_beta_key — attach scopes after redeem
                      POST /api/auth/refresh_session — rebake cookie with live extras

  Admin pages         GET  /app/premium              — minimal HTML console
                      GET  /app/beta_keys            — minimal HTML console

Auth model
----------
Owner routes go through the Mini App's existing `_verify` + `_require_owner`
chain, so they work via initData *and* via the owner v1 cookie. The redeem +
refresh endpoints only need a verified caller; they fail with 401 for
anonymous requests (NO silent identity creation).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import auth_v2, beta_keys, entitlements, grant_transport, premium, profile
from .auth_google import (
    COMMUNITY_USER_SCOPES, _SESSION_COOKIE, _SESSION_COOKIE_DOMAIN,
    _SESSION_COOKIE_TTL_SEC, _signing_secret,
)
from .grant_transport import VIEW_AS_COOKIE
from .miniapp import _require_owner, _verify


router = APIRouter()


# ── helpers ─────────────────────────────────────────────────────────────────


def _session_user_id(payload: dict) -> str | None:
    """Pull the canonical user_id from a verified _verify() payload.

    The session block is set by _verify for both cookie- and initData-auth
    paths. Owner gets "owner"; community Telegram users get their chat_id
    (digit string); Google users carry "google:<sub>"."""
    sess = (payload or {}).get("session") or {}
    uid = sess.get("user_id") or ""
    if uid:
        return str(uid)
    # initData fall-through — synthesise from payload.user.id when the
    # session block is missing.
    u = (payload or {}).get("user") or {}
    if u.get("id"):
        return str(u["id"])
    return None


def _set_session_cookie(response: Response, cookie_value: str) -> None:
    response.set_cookie(
        _SESSION_COOKIE, cookie_value,
        max_age=_SESSION_COOKIE_TTL_SEC,
        httponly=True, secure=True, samesite="lax",
        domain=_SESSION_COOKIE_DOMAIN, path="/",
    )


# ── Owner CRUD — premium users ──────────────────────────────────────────────


class PremiumAddBody(BaseModel):
    identity_type: str
    identity_value: str
    plan: str
    notes: Optional[str] = None
    expires_at: Optional[str] = None


@router.get("/api/admin/premium")
async def admin_premium_list(request: Request):
    p = await _verify(request)
    _require_owner(p)
    rows = await premium.list_all()
    return {"premium_users": rows, "plans": list(entitlements.PLANS.keys())}


@router.post("/api/admin/premium")
async def admin_premium_add(body: PremiumAddBody, request: Request):
    p = await _verify(request)
    _require_owner(p)
    try:
        row = await premium.add(
            body.identity_type, body.identity_value, body.plan,
            notes=body.notes, expires_at=body.expires_at,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "row": row}


@router.delete("/api/admin/premium")
async def admin_premium_remove(request: Request,
                                identity_type: str, identity_value: str):
    p = await _verify(request)
    _require_owner(p)
    try:
        removed = await premium.remove(identity_type, identity_value)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "removed": removed}


# ── Owner: "preview as <plan>" simulation + capability catalog ───────────────


class ViewAsBody(BaseModel):
    tier: str   # free | registered | plus | family | owner (owner = clear)


@router.get("/api/admin/view_as")
async def admin_view_as_get(request: Request):
    p = await _verify(request)
    _require_owner(p)
    raw = (request.cookies.get(VIEW_AS_COOKIE) or "").strip().lower()
    tier = raw if raw in entitlements.PLANS else "owner"
    return {"tier": tier, "plans": list(entitlements.PLANS.keys())}


@router.post("/api/admin/view_as")
async def admin_view_as_set(body: ViewAsBody, request: Request, response: Response):
    """Owner-only. Set (or clear) the preview-as plan cookie. 'owner' or any
    unknown value clears it → back to the full owner grant. Downgrade-only: the
    grant layer honours the cookie solely for the owner identity, so it can
    never escalate anyone."""
    p = await _verify(request)
    _require_owner(p)
    tier = (body.tier or "owner").strip().lower()
    if tier == "owner" or tier not in entitlements.PLANS:
        response.delete_cookie(VIEW_AS_COOKIE, domain=_SESSION_COOKIE_DOMAIN, path="/")
        return {"ok": True, "tier": "owner"}
    response.set_cookie(
        VIEW_AS_COOKIE, tier,
        max_age=_SESSION_COOKIE_TTL_SEC,
        httponly=False, secure=True, samesite="lax",
        domain=_SESSION_COOKIE_DOMAIN, path="/",
    )
    return {"ok": True, "tier": tier}


@router.get("/api/entitlements/catalog")
async def entitlements_catalog(request: Request):
    """Capability catalog overlaid with the caller's CURRENT (possibly
    simulated) grant: which caps are unlocked, the plan needed for the rest,
    whether enforcement is active, and the purchase rail. Any signed-in caller
    (not owner-only) — drives the lock badges + upsell sheet."""
    await _verify(request)   # 401/403 for anonymous; valid session required
    grant = await grant_transport.resolve_grant(request)
    caps = [
        {**item, "unlocked": entitlements.has_entitlement(grant, item["cap"])}
        for item in entitlements.catalog()
    ]
    simulating = grant.get("source") == "owner_simulation"
    return {
        "plan": grant.get("plan", "free"),
        "simulating": simulating,
        "enforced": grant_transport.enforcement_active() or simulating,
        "rail": profile.billing_rail(),
        "caps": caps,
    }


# ── Owner CRUD — beta keys ──────────────────────────────────────────────────


class BetaKeyMintBody(BaseModel):
    label: Optional[str] = None
    extra_scopes: list[str]
    expires_at: Optional[str] = None
    note: Optional[str] = None


@router.get("/api/admin/beta_keys")
async def admin_beta_list(request: Request):
    p = await _verify(request)
    _require_owner(p)
    rows = await beta_keys.list_keys()
    # Don't surface secret_hash — it's an HMAC but a defence-in-depth no-op.
    sanitised = [{k: v for k, v in r.items() if k != "secret_hash"} for r in rows]
    return {"beta_keys": sanitised}


@router.post("/api/admin/beta_keys")
async def admin_beta_mint(body: BetaKeyMintBody, request: Request):
    p = await _verify(request)
    _require_owner(p)
    creator = int((p.get("user") or {}).get("id") or 0) or None
    try:
        row = await beta_keys.mint(
            body.label or "", body.extra_scopes,
            expires_at=body.expires_at, created_by=creator, note=body.note,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    # `key` (plaintext) returned ONCE — caller responsible for surfacing.
    return {"ok": True, "row": row}


@router.delete("/api/admin/beta_keys/{key_id}")
async def admin_beta_revoke(key_id: str, request: Request):
    p = await _verify(request)
    _require_owner(p)
    revoked = await beta_keys.revoke(key_id)
    return {"ok": True, "revoked": revoked}


# ── User self-service — beta-key redeem + session refresh ───────────────────


class RedeemBody(BaseModel):
    key: str


@router.post("/api/auth/redeem_beta_key")
async def auth_redeem_beta_key(body: RedeemBody, request: Request, response: Response):
    """Attach the key's extra scopes to the caller's session. Identity is
    NEVER swapped — the caller must already be signed in; redemption only
    pins the key to that identity and rebakes the cookie."""
    p = await _verify(request)
    user_id = _session_user_id(p)
    if not user_id:
        raise HTTPException(401, "sign_in_required")
    try:
        await beta_keys.redeem(body.key, user_id)
    except beta_keys.RedeemError as e:
        # Map known codes to HTTP statuses.
        if e.code in ("invalid_format", "bad_signature", "unknown_key"):
            raise HTTPException(400, e.code)
        if e.code in ("revoked", "expired", "already_redeemed"):
            raise HTTPException(409, e.code)
        if e.code == "sign_in_required":
            raise HTTPException(401, e.code)
        raise HTTPException(400, e.code)
    # Rebake the cookie so the new scopes are live without re-login. Only
    # rebake for cookie-auth callers — initData callers have no cookie to
    # refresh (their scopes are synthesised per-request by _verify, and a
    # subsequent _verify call already picks up live beta scopes via the
    # session-issue path; see _verify wiring below).
    new_scopes = await _rebake_cookie_if_present(user_id, request, response)
    return {"ok": True, "scopes": new_scopes}


@router.post("/api/auth/refresh_session")
async def auth_refresh_session(request: Request, response: Response):
    """Idempotent: re-issue the session cookie merging in any newly-redeemed
    beta scopes. Useful after the owner mints+hands a key to a signed-in
    user out-of-band (e.g. via Telegram DM) without a full re-login."""
    p = await _verify(request)
    user_id = _session_user_id(p)
    if not user_id:
        raise HTTPException(401, "sign_in_required")
    new_scopes = await _rebake_cookie_if_present(user_id, request, response)
    return {"ok": True, "scopes": new_scopes}


async def _rebake_cookie_if_present(user_id: str, request: Request,
                                     response: Response) -> list[str]:
    """If the caller has a session cookie, re-issue it with COMMUNITY_USER_SCOPES
    ∪ live beta extras. Owner cookies are left untouched (they're v1 wildcard;
    rebaking would downgrade them). Returns the effective scope list."""
    extras = await beta_keys.live_extra_scopes_for(user_id)
    if user_id == "owner":
        # Owner already has wildcard — extras are irrelevant.
        return ["*"]
    scopes = sorted(set(COMMUNITY_USER_SCOPES) | set(extras))
    # Only rebake the cookie when the caller actually presented one — initData
    # callers don't need a cookie set.
    cookie_present = bool(request.cookies.get(_SESSION_COOKIE))
    if cookie_present:
        secret = _signing_secret()
        if secret:
            cookie = auth_v2.issue_v2_cookie(secret, user_id, scopes)
            _set_session_cookie(response, cookie)
    return scopes


# ── Minimal admin pages (owner-only via Mini App auth) ──────────────────────


_PREMIUM_HTML = """<!doctype html>
<meta charset="utf-8"><title>Premium users · SMDL</title>
<style>
  body { font: 14px/1.5 -apple-system,Segoe UI,sans-serif; background:#0e1117; color:#e6e9ef; margin:0; padding:24px; }
  .wrap { max-width:880px; margin:0 auto; }
  a.back { color:#7fb8ff; text-decoration:none; font-size:13px; }
  h1 { margin:8px 0 6px; font-size:22px; }
  p.sub { color:#9aa3ad; margin:0 0 18px; }
  .card { background:#171b22; border:1px solid #232a33; border-radius:10px; padding:16px; margin-bottom:14px; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th, td { text-align:left; padding:8px 6px; border-bottom:1px solid #232a33; }
  th { color:#9aa3ad; font-weight:600; }
  button { background:#2563eb; color:white; border:0; padding:8px 14px; border-radius:6px; cursor:pointer; font-size:13px; }
  button.ghost { background:#232a33; color:#e6e9ef; }
  input, select { background:#0e1117; border:1px solid #232a33; color:#e6e9ef; padding:6px 8px; border-radius:6px; font-size:13px; }
  .row { display:grid; grid-template-columns:1fr 1fr 1fr; gap:10px; margin-bottom:10px; }
</style>
<div class="wrap">
  <a class="back" href="/app">&larr; Back to app</a>
  <h1>Premium users</h1>
  <p class="sub">Mark an identity (Telegram chat_id, Google sub, or e-mail) as plus / family / registered. The plan is baked into every grant for that identity without going through the license-key rail.</p>
  <div class="card">
    <h3 style="margin:0 0 10px">Add</h3>
    <div class="row">
      <select id="f-type">
        <option value="telegram">Telegram chat_id</option>
        <option value="google">Google sub</option>
        <option value="email">E-mail</option>
      </select>
      <input id="f-value" placeholder="identity value">
      <select id="f-plan">
        <option value="registered">registered (free account)</option>
        <option value="plus" selected>plus</option>
        <option value="family">family</option>
      </select>
    </div>
    <div class="row">
      <input id="f-notes" placeholder="notes (optional)">
      <input id="f-expires" placeholder="expires ISO (optional)">
      <button id="btn-add">Add</button>
    </div>
  </div>
  <div class="card">
    <h3 style="margin:0 0 10px">Current</h3>
    <div id="host">Loading…</div>
  </div>
</div>
<script>
const tg = window.Telegram && window.Telegram.WebApp;
if (tg) { try { tg.ready(); tg.expand(); } catch(e){} }
const init = (tg && tg.initData) || '';
async function api(p, opts){ opts=opts||{}; opts.headers=Object.assign({},opts.headers||{},{'X-Init-Data':init}, opts.body?{'Content-Type':'application/json'}:{}); const r=await fetch(p,opts); const t=await r.text(); let d={}; try{d=JSON.parse(t);}catch(e){} if(!r.ok) throw new Error(d.detail||r.status); return d; }
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);}
async function load(){
  const host=document.getElementById('host');
  try{
    const res=await api('/api/admin/premium');
    const rows=res.premium_users||[];
    if(!rows.length){ host.innerHTML='<p style="color:#6b7280">None yet.</p>'; return; }
    let h='<table><thead><tr><th>Identity</th><th>Plan</th><th>Notes</th><th>Expires</th><th></th></tr></thead><tbody>';
    for(const r of rows){
      h+='<tr><td>'+esc(r.identity_type)+': '+esc(r.identity_value)+'</td><td>'+esc(r.plan)+'</td><td>'+esc(r.notes||'')+'</td><td>'+esc(r.expires_at||'—')+'</td>'+
         '<td><button class="ghost" data-t="'+esc(r.identity_type)+'" data-v="'+esc(r.identity_value)+'">Remove</button></td></tr>';
    }
    h+='</tbody></table>'; host.innerHTML=h;
    host.querySelectorAll('button').forEach(b=>b.onclick=async()=>{
      if(!confirm('Remove?')) return;
      await api('/api/admin/premium?identity_type='+encodeURIComponent(b.dataset.t)+'&identity_value='+encodeURIComponent(b.dataset.v),{method:'DELETE'});
      load();
    });
  } catch(e){ host.textContent='Error: '+e.message; }
}
document.getElementById('btn-add').onclick=async()=>{
  const body={identity_type:document.getElementById('f-type').value,identity_value:document.getElementById('f-value').value.trim(),plan:document.getElementById('f-plan').value,notes:document.getElementById('f-notes').value.trim()||null,expires_at:document.getElementById('f-expires').value.trim()||null};
  if(!body.identity_value) return alert('identity value is required');
  try{ await api('/api/admin/premium',{method:'POST',body:JSON.stringify(body)}); document.getElementById('f-value').value=''; load(); } catch(e){ alert(e.message); }
};
load();
</script>"""


_BETA_HTML = """<!doctype html>
<meta charset="utf-8"><title>Beta keys · SMDL</title>
<style>
  body { font: 14px/1.5 -apple-system,Segoe UI,sans-serif; background:#0e1117; color:#e6e9ef; margin:0; padding:24px; }
  .wrap { max-width:880px; margin:0 auto; }
  a.back { color:#7fb8ff; text-decoration:none; font-size:13px; }
  h1 { margin:8px 0 6px; font-size:22px; }
  p.sub { color:#9aa3ad; margin:0 0 18px; }
  .card { background:#171b22; border:1px solid #232a33; border-radius:10px; padding:16px; margin-bottom:14px; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th, td { text-align:left; padding:8px 6px; border-bottom:1px solid #232a33; }
  th { color:#9aa3ad; font-weight:600; }
  button { background:#2563eb; color:white; border:0; padding:8px 14px; border-radius:6px; cursor:pointer; font-size:13px; }
  button.ghost { background:#232a33; color:#e6e9ef; }
  input { background:#0e1117; border:1px solid #232a33; color:#e6e9ef; padding:6px 8px; border-radius:6px; font-size:13px; width:100%; box-sizing:border-box; }
  .row { display:grid; grid-template-columns:1fr 2fr 1fr 1fr; gap:10px; margin-bottom:10px; align-items:end; }
  code.key { background:#0e1117; border:1px solid #f6b441; color:#f6b441; padding:8px 12px; border-radius:6px; font-family:monospace; word-break:break-all; display:block; }
</style>
<div class="wrap">
  <a class="back" href="/app">&larr; Back to app</a>
  <h1>Beta keys</h1>
  <p class="sub">Issue keys that grant named extra scopes to whoever redeems them. Different from license keys: these don't change the user's plan — they only attach scopes to that user's session for as long as the key is live.</p>
  <div class="card">
    <h3 style="margin:0 0 10px">Mint</h3>
    <div class="row">
      <div><label>Label</label><input id="f-label" placeholder="e.g. recorder beta — Ali"></div>
      <div><label>Extra scopes (comma-sep)</label><input id="f-scopes" placeholder="smdl.tv.recorder.beta, smdl.foo.bar"></div>
      <div><label>Expires (ISO)</label><input id="f-exp" placeholder="optional"></div>
      <div><button id="btn-mint">Mint key</button></div>
    </div>
    <div id="last-key" style="display:none; margin-top:14px">
      <div style="margin-bottom:6px; color:#f6b441;">New key — copy it now, never shown again:</div>
      <code class="key" id="last-key-code"></code>
    </div>
  </div>
  <div class="card">
    <h3 style="margin:0 0 10px">Current</h3>
    <div id="host">Loading…</div>
  </div>
</div>
<script>
const tg = window.Telegram && window.Telegram.WebApp;
if (tg) { try { tg.ready(); tg.expand(); } catch(e){} }
const init = (tg && tg.initData) || '';
async function api(p, opts){ opts=opts||{}; opts.headers=Object.assign({},opts.headers||{},{'X-Init-Data':init}, opts.body?{'Content-Type':'application/json'}:{}); const r=await fetch(p,opts); const t=await r.text(); let d={}; try{d=JSON.parse(t);}catch(e){} if(!r.ok) throw new Error(d.detail||r.status); return d; }
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);}
async function load(){
  const host=document.getElementById('host');
  try{
    const res=await api('/api/admin/beta_keys');
    const rows=res.beta_keys||[];
    if(!rows.length){ host.innerHTML='<p style="color:#6b7280">None yet.</p>'; return; }
    let h='<table><thead><tr><th>Label</th><th>Scopes</th><th>Redeemer</th><th>Status</th><th>Expires</th><th></th></tr></thead><tbody>';
    for(const r of rows){
      const status = r.revoked_at ? 'revoked' : (r.redeemed_by_user_id ? 'redeemed' : 'unredeemed');
      h+='<tr><td>'+esc(r.label||r.key_id)+'</td><td>'+esc((r.extra_scopes||[]).join(', '))+'</td><td>'+esc(r.redeemed_by_user_id||'—')+'</td><td>'+esc(status)+'</td><td>'+esc(r.expires_at||'—')+'</td>'+
         '<td>'+(r.revoked_at?'':'<button class="ghost" data-k="'+esc(r.key_id)+'">Revoke</button>')+'</td></tr>';
    }
    h+='</tbody></table>'; host.innerHTML=h;
    host.querySelectorAll('button').forEach(b=>b.onclick=async()=>{
      if(!confirm('Revoke this key?')) return;
      await api('/api/admin/beta_keys/'+encodeURIComponent(b.dataset.k),{method:'DELETE'});
      load();
    });
  } catch(e){ host.textContent='Error: '+e.message; }
}
document.getElementById('btn-mint').onclick=async()=>{
  const body={label:document.getElementById('f-label').value.trim()||null,extra_scopes:document.getElementById('f-scopes').value.split(',').map(s=>s.trim()).filter(Boolean),expires_at:document.getElementById('f-exp').value.trim()||null};
  if(!body.extra_scopes.length) return alert('At least one scope required');
  try{
    const res=await api('/api/admin/beta_keys',{method:'POST',body:JSON.stringify(body)});
    document.getElementById('last-key').style.display='block';
    document.getElementById('last-key-code').textContent=res.row.key;
    load();
  } catch(e){ alert(e.message); }
};
load();
</script>"""


@router.get("/app/premium", response_class=HTMLResponse)
async def page_premium():
    return HTMLResponse(_PREMIUM_HTML)


@router.get("/app/beta_keys", response_class=HTMLResponse)
async def page_beta_keys():
    return HTMLResponse(_BETA_HTML)
