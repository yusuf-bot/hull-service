"""Hull tunnel service — OAuth-protected tunnels for hull MCP servers.

Auth model:
- Each tunnel has allowed_emails (optional JSON array)
- If allowed_emails is set, Claude Desktop does OAuth flow
- hull-service verifies the email is in the list before granting access
- If no allowed_emails, tunnel is open (anyone with URL can access)
"""

import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlencode

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException, Form
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ─── Config ──────────────────────────────────────────────────────────────────

DB_PATH = Path(os.environ.get("HULL_DB_PATH", "/var/lib/hull/tunnels.db"))
SECRET_KEY = os.environ.get("HULL_SECRET_KEY", secrets.token_hex(32))
BASE_URL = os.environ.get("HULL_BASE_URL", "https://hull.openexplorer.xyz")
DEFAULT_TTL = 3600  # 1 hour
MAX_TTL = 86400     # 24 hours


# ─── Database ────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS tunnels (
    tunnel_id TEXT PRIMARY KEY,
    url_token TEXT UNIQUE,
    allowed_emails TEXT,
    client_id TEXT,
    client_secret_hash TEXT,
    machine_name TEXT,
    created_at TEXT,
    expires_at TEXT,
    active INTEGER DEFAULT 1,
    requests_served INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS oauth_codes (
    code TEXT PRIMARY KEY,
    tunnel_id TEXT,
    email TEXT,
    created_at TEXT,
    expires_at TEXT,
    used INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS oauth_tokens (
    token_hash TEXT PRIMARY KEY,
    tunnel_id TEXT,
    email TEXT,
    created_at TEXT,
    expires_at TEXT
);
"""


def get_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db():
    conn = get_db()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ─── Models ──────────────────────────────────────────────────────────────────

class TunnelCreate(BaseModel):
    machine_name: str
    ttl_seconds: int = DEFAULT_TTL
    allowed_emails: list[str] | None = None


class TunnelResponse(BaseModel):
    tunnel_id: str
    url: str
    client_id: str
    client_secret: str
    expires_at: str
    ttl_seconds: int
    allowed_emails: list[str] | None = None


# ─── App ─────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(title="Hull Service", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Active tunnel connections: tunnel_id -> WebSocket
active_tunnels: dict[str, WebSocket] = {}


# ─── API Endpoints ──────────────────────────────────────────────────────────

@app.post("/api/tunnel/create", response_model=TunnelResponse)
async def create_tunnel(req: TunnelCreate):
    """Create a new tunnel and return the public URL."""
    ttl = min(req.ttl_seconds, MAX_TTL)
    tunnel_id = secrets.token_urlsafe(8)
    url_token = secrets.token_urlsafe(16)
    expires_at = datetime.fromtimestamp(
        time.time() + ttl, tz=timezone.utc
    ).isoformat()

    # Generate OAuth client credentials for this tunnel
    client_id = f"hull-{tunnel_id}"
    client_secret = secrets.token_urlsafe(32)
    client_secret_hash = _hash(client_secret)

    # Store emails as JSON array
    emails_json = json.dumps(req.allowed_emails) if req.allowed_emails else None

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO tunnels (tunnel_id, url_token, allowed_emails, client_id, client_secret_hash, machine_name, created_at, expires_at, active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (tunnel_id, url_token, emails_json, client_id, client_secret_hash, req.machine_name,
             datetime.now(timezone.utc).isoformat(), expires_at),
        )
        conn.commit()
    finally:
        conn.close()

    return TunnelResponse(
        tunnel_id=tunnel_id,
        url=f"{BASE_URL}/{url_token}",
        client_id=client_id,
        client_secret=client_secret,
        expires_at=expires_at,
        ttl_seconds=ttl,
        allowed_emails=req.allowed_emails,
    )


@app.get("/api/tunnel/{tunnel_id}/status")
async def tunnel_status(tunnel_id: str):
    """Check if a tunnel is active."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM tunnels WHERE tunnel_id = ?", (tunnel_id,)
        ).fetchone()
    finally:
        conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="Tunnel not found")

    is_expired = row["expires_at"] < datetime.now(timezone.utc).isoformat()
    if is_expired and row["active"]:
        conn = get_db()
        try:
            conn.execute("UPDATE tunnels SET active = 0 WHERE tunnel_id = ?", (tunnel_id,))
            conn.commit()
        finally:
            conn.close()

    allowed_emails = json.loads(row["allowed_emails"]) if row["allowed_emails"] else None

    return {
        "tunnel_id": row["tunnel_id"],
        "machine_name": row["machine_name"],
        "active": row["active"] and not is_expired,
        "expires_at": row["expires_at"],
        "requests_served": row["requests_served"],
        "allowed_emails": allowed_emails,
    }


@app.delete("/api/tunnel/{tunnel_id}")
async def close_tunnel(tunnel_id: str):
    """Close a tunnel early."""
    conn = get_db()
    try:
        cur = conn.execute(
            "UPDATE tunnels SET active = 0 WHERE tunnel_id = ?", (tunnel_id,)
        )
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Tunnel not found")
    finally:
        conn.close()

    ws = active_tunnels.pop(tunnel_id, None)
    if ws:
        await ws.close()

    return {"status": "closed"}


@app.websocket("/ws/{tunnel_id}")
async def tunnel_websocket(websocket: WebSocket, tunnel_id: str):
    """WebSocket endpoint for hull servers to connect to."""
    await websocket.accept()
    active_tunnels[tunnel_id] = websocket

    try:
        while True:
            data = await websocket.receive_text()
    except WebSocketDisconnect:
        active_tunnels.pop(tunnel_id, None)


# ─── OAuth Endpoints ─────────────────────────────────────────────────────────

@app.get("/.well-known/oauth-authorization-server")
async def oauth_metadata():
    """OAuth metadata for MCP clients."""
    return {
        "issuer": BASE_URL,
        "authorization_endpoint": f"{BASE_URL}/oauth/authorize",
        "token_endpoint": f"{BASE_URL}/oauth/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "token_endpoint_auth_methods_supported": ["client_secret_post"],
    }


@app.get("/oauth/authorize")
async def oauth_authorize(
    response_type: str,
    client_id: str,
    redirect_uri: str,
    state: str,
    tunnel_id: str = None,
):
    """OAuth authorization endpoint — shows login page."""
    if not tunnel_id:
        if client_id.startswith("hull-"):
            tunnel_id = client_id[5:]
        else:
            raise HTTPException(status_code=400, detail="Invalid client_id format")

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM tunnels WHERE tunnel_id = ? AND active = 1",
            (tunnel_id,),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="Tunnel not found or expired")

    if row["expires_at"] < datetime.now(timezone.utc).isoformat():
        raise HTTPException(status_code=410, detail="Tunnel expired")

    allowed_emails = json.loads(row["allowed_emails"]) if row["allowed_emails"] else None

    # Build login page
    emails_info = ""
    if allowed_emails:
        email_list = ", ".join(f"<strong>{e}</strong>" for e in allowed_emails)
        emails_info = f"<div class='info'>Only {email_list} can access this tunnel.</div>"

    login_html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Hull — Sign In</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; 
               display: flex; justify-content: center; align-items: center; min-height: 100vh; 
               margin: 0; background: #f5f5f5; }}
        .card {{ background: white; padding: 2rem; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1);
                max-width: 400px; width: 100%; }}
        h1 {{ margin: 0 0 0.5rem 0; font-size: 1.5rem; }}
        .subtitle {{ color: #666; margin-bottom: 1.5rem; }}
        .info {{ background: #f0f7ff; padding: 1rem; border-radius: 4px; margin-bottom: 1.5rem; font-size: 0.9rem; }}
        input {{ width: 100%; padding: 0.75rem; border: 1px solid #ddd; border-radius: 4px; 
                font-size: 1rem; box-sizing: border-box; margin-bottom: 1rem; }}
        button {{ width: 100%; padding: 0.75rem; background: #000; color: white; border: none; 
                 border-radius: 4px; font-size: 1rem; cursor: pointer; }}
        button:hover {{ background: #333; }}
        .error {{ color: #d32f2f; margin-bottom: 1rem; }}
    </style>
</head>
<body>
    <div class="card">
        <h1>Hull</h1>
        <p class="subtitle">Sign in to access remote machine</p>
        
        {emails_info}
        
        <form method="POST" action="/oauth/authorize">
            <input type="hidden" name="client_id" value="{client_id}">
            <input type="hidden" name="redirect_uri" value="{redirect_uri}">
            <input type="hidden" name="state" value="{state}">
            <input type="hidden" name="tunnel_id" value="{tunnel_id}">
            
            <input type="email" name="email" placeholder="Email address" required autofocus>
            <button type="submit">Continue</button>
        </form>
    </div>
</body>
</html>"""

    return HTMLResponse(content=login_html)


@app.post("/oauth/authorize")
async def oauth_authorize_post(
    email: str = Form(...),
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    state: str = Form(...),
    tunnel_id: str = Form(None),
):
    """Handle OAuth authorization — verify email and redirect."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM tunnels WHERE tunnel_id = ? AND active = 1",
            (tunnel_id,),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="Tunnel not found")

    allowed_emails = json.loads(row["allowed_emails"]) if row["allowed_emails"] else None

    # Check email if restricted
    if allowed_emails and email.lower() not in [e.lower() for e in allowed_emails]:
        email_list = ", ".join(f"<strong>{e}</strong>" for e in allowed_emails)
        error_html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Hull — Access Denied</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; 
               display: flex; justify-content: center; align-items: center; min-height: 100vh; 
               margin: 0; background: #f5f5f5; }}
        .card {{ background: white; padding: 2rem; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1);
                max-width: 400px; width: 100%; text-align: center; }}
        .error {{ color: #d32f2f; font-size: 1.1rem; margin-bottom: 1rem; }}
        a {{ color: #0066cc; text-decoration: none; }}
    </style>
</head>
<body>
    <div class="card">
        <div class="error">Access Denied</div>
        <p>You signed in as <strong>{email}</strong></p>
        <p>This tunnel only allows: {email_list}</p>
        <p><a href="javascript:history.back()">Try again</a></p>
    </div>
</body>
</html>"""
        return HTMLResponse(content=error_html, status_code=403)

    # Generate authorization code
    code = secrets.token_urlsafe(32)
    code_hash = _hash(code)
    expires_at = datetime.fromtimestamp(
        time.time() + 600, tz=timezone.utc  # 10 minutes
    ).isoformat()

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO oauth_codes (code, tunnel_id, email, created_at, expires_at, used) "
            "VALUES (?, ?, ?, ?, ?, 0)",
            (code_hash, tunnel_id, email.lower(),
             datetime.now(timezone.utc).isoformat(), expires_at),
        )
        conn.commit()
    finally:
        conn.close()

    # Redirect back to Claude with code
    redirect_params = {
        "code": code,
        "state": state,
    }
    redirect_url = f"{redirect_uri}?{urlencode(redirect_params)}"

    return RedirectResponse(url=redirect_url, status_code=302)


@app.post("/oauth/token")
async def oauth_token(request: Request):
    """Exchange authorization code for access token."""
    body = await request.form()
    grant_type = body.get("grant_type")
    code = body.get("code")
    client_id = body.get("client_id")
    client_secret = body.get("client_secret")

    if grant_type != "authorization_code":
        raise HTTPException(status_code=400, detail="unsupported_grant_type")

    if not code or not client_id:
        raise HTTPException(status_code=400, detail="invalid_request")

    # Verify client_id and client_secret
    client_secret_hash = _hash(client_secret) if client_secret else None
    conn = get_db()
    try:
        tunnel_row = conn.execute(
            "SELECT * FROM tunnels WHERE client_id = ? AND client_secret_hash = ?",
            (client_id, client_secret_hash),
        ).fetchone()
    finally:
        conn.close()

    if not tunnel_row:
        raise HTTPException(status_code=401, detail="invalid_client")

    # Verify code
    code_hash = _hash(code)
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM oauth_codes WHERE code = ? AND used = 0 AND tunnel_id = ?",
            (code_hash, tunnel_row["tunnel_id"]),
        ).fetchone()

        if not row:
            raise HTTPException(status_code=400, detail="invalid_grant")

        if row["expires_at"] < datetime.now(timezone.utc).isoformat():
            raise HTTPException(status_code=400, detail="expired_grant")

        # Mark code as used
        conn.execute(
            "UPDATE oauth_codes SET used = 1 WHERE code = ?", (code_hash,)
        )

        # Generate access token
        access_token = secrets.token_urlsafe(32)
        token_hash = _hash(access_token)
        token_expires = datetime.fromtimestamp(
            time.time() + 3600, tz=timezone.utc  # 1 hour
        ).isoformat()

        conn.execute(
            "INSERT INTO oauth_tokens (token_hash, tunnel_id, email, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (token_hash, row["tunnel_id"], row["email"],
             datetime.now(timezone.utc).isoformat(), token_expires),
        )
        conn.commit()

    finally:
        conn.close()

    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": 3600,
    }


@app.get("/oauth/userinfo")
async def oauth_userinfo(request: Request):
    """Return user info for verified token."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing_token")

    token = auth_header[7:]
    token_hash = _hash(token)

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM oauth_tokens WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        raise HTTPException(status_code=401, detail="invalid_token")

    if row["expires_at"] < datetime.now(timezone.utc).isoformat():
        raise HTTPException(status_code=401, detail="expired_token")

    return {
        "sub": row["email"],
        "email": row["email"],
        "email_verified": True,
    }


# ─── Proxy ──────────────────────────────────────────────────────────────────

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_mcp(request: Request, path: str):
    """Proxy MCP requests through the tunnel to the user's hull server."""
    parts = path.split("/", 1) if path else []
    url_token = parts[0] if parts else ""

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM tunnels WHERE url_token = ? AND active = 1",
            (url_token,),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="Tunnel not found or expired")

    if row["expires_at"] < datetime.now(timezone.utc).isoformat():
        raise HTTPException(status_code=410, detail="Tunnel expired")

    # Check auth if emails are restricted
    allowed_emails = json.loads(row["allowed_emails"]) if row["allowed_emails"] else None
    if allowed_emails:
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Authentication required")

        token = auth_header[7:]
        token_hash = _hash(token)

        conn = get_db()
        try:
            token_row = conn.execute(
                "SELECT * FROM oauth_tokens WHERE token_hash = ? AND tunnel_id = ?",
                (token_hash, row["tunnel_id"]),
            ).fetchone()
        finally:
            conn.close()

        if not token_row:
            raise HTTPException(status_code=403, detail="Invalid token for this tunnel")

        if token_row["expires_at"] < datetime.now(timezone.utc).isoformat():
            raise HTTPException(status_code=401, detail="Token expired")

        if token_row["email"] not in [e.lower() for e in allowed_emails]:
            raise HTTPException(status_code=403, detail="Email not authorized")

    # Check websocket connection
    ws = active_tunnels.get(row["tunnel_id"])
    if not ws:
        raise HTTPException(status_code=503, detail="Hull server not connected")

    # Increment request counter
    conn = get_db()
    try:
        conn.execute(
            "UPDATE tunnels SET requests_served = requests_served + 1 WHERE tunnel_id = ?",
            (row["tunnel_id"],),
        )
        conn.commit()
    finally:
        conn.close()

    # Forward request through WebSocket (placeholder)
    return JSONResponse({
        "status": "ok",
        "message": f"Request forwarded to tunnel {row['tunnel_id']}",
    })


@app.get("/health")
async def health():
    return {"status": "ok", "service": "hull"}
