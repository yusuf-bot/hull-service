# Hull Service

Tunnel service for [hull](https://github.com/yourname/hull) MCP servers.
Generates temporary public URLs so remote AI agents can connect to any machine.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  User's Machine (Windows/Mac/Linux)                         │
│                                                             │
│  $ hull run --remote                                        │
│  ┌──────────────┐     ┌──────────────┐                      │
│  │ Hull Server  │────▶│ Tunnel Client │────WebSocket────┐   │
│  │ (localhost)  │     │              │                  │   │
│  └──────────────┘     └──────────────┘                  │   │
└───────────────────────────────────────────────────────────┘ │
                        │
                        ▼
┌───────────────────────────────────────────────────────────┐
│  hull.openexplorer.xyz (Ubuntu)                           │
│                                                           │
│  ┌──────────────┐     ┌──────────────┐                    │
│  │ API Server   │────▶│ Tunnel Relay  │                    │
│  │ (FastAPI)    │     │ (WebSocket)   │                    │
│  └──────────────┘     └──────────────┘                    │
│         │                     │                           │
│         ▼                     ▼                           │
│  ┌──────────────┐     ┌──────────────┐                    │
│  │ URL Store    │     │ Proxy        │                    │
│  │ (SQLite)     │     │ (HTTP→WS)    │                    │
│  └──────────────┘     └──────────────┘                    │
│                         │                                 │
└─────────────────────────┼─────────────────────────────────┘
                          │
                          ▼ HTTPS
┌───────────────────────────────────────────────────────────┐
│  Claude Desktop / ChatGPT                                 │
│                                                           │
│  Custom Connector URL:                                    │
│  https://hull.openexplorer.xyz/a1b2c3d4                  │
└───────────────────────────────────────────────────────────┘
```

## Flow

1. User runs `hull run --remote` on their machine
2. Hull starts local HTTP server on port 7999
3. Hull connects to hull.openexplorer.xyz via WebSocket
4. Service generates temporary URL: `hull.openexplorer.xyz/<uuid>`
5. Service prints URL to user's terminal
6. User adds URL to Claude Desktop custom connector
7. Claude Desktop sends MCP requests to the public URL
8. Service proxies requests through WebSocket to user's local hull server
9. Hull processes and returns responses through the tunnel

## API Endpoints

### POST /api/tunnel/create
Creates a new tunnel session.

**Request:**
```json
{
  "machine_name": "yusufs-macbook",
  "ttl_seconds": 3600
}
```

**Response:**
```json
{
  "tunnel_id": "a1b2c3d4",
  "url": "https://hull.openexplorer.xyz/a1b2c3d4",
  "expires_at": "2026-08-17T22:45:00Z",
  "websocket_url": "wss://hull.openexplorer.xyz/ws/a1b2c3d4"
}
```

### WebSocket /ws/<tunnel_id>
Persistent connection for tunneling MCP traffic.

### GET /api/tunnel/<tunnel_id>/status
Check tunnel status.

### DELETE /api/tunnel/<tunnel_id>
Close tunnel early.

## Setup (Ubuntu)

```bash
# Install
git clone https://github.com/yourname/hull-service
cd hull-service
pip install -r requirements.txt

# Configure
export HULL_SECRET_KEY="your-secret-key"
export HULL_PORT=443
export HULL_DB_PATH="/var/lib/hull/tunnels.db"

# Run
uvicorn app:app --host 0.0.0.0 --port 443 --ssl-keyfile=key.pem --ssl-certfile=cert.pem
```

## Nginx Config

```nginx
server {
    listen 443 ssl;
    server_name hull.openexplorer.xyz;

    ssl_certificate /etc/letsencrypt/live/hull.openexplorer.xyz/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/hull.openexplorer.xyz/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}

server {
    listen 80;
    server_name hull.openexplorer.xyz;
    return 301 https://$host$request_uri;
}
```

## Tech Stack

- **FastAPI** — API + WebSocket handling
- **SQLite** — tunnel URL storage (simple, no Redis needed)
- **websockets** — tunnel relay
- **Certbot** — SSL certificates
- **Nginx** — reverse proxy + SSL termination

## Free Tier Limits

- 1 tunnel per machine
- 1 hour TTL (renewable)
- 10MB max request/response size
- Rate limited to 100 requests/minute
