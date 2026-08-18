# Hull Service — VPS Deployment Guide

This guide covers deploying `hull-service` on your VPS so remote users can
connect through tunnels to hull-openexplorer.xyz.

## Prerequisites

- VPS running Ubuntu 22.04+ (or any Linux with Docker)
- Domain pointed at VPS (e.g., `hull.openexplorer.xyz` → VPS IP)
- Ports 80 and 443 open
- Docker + Docker Compose installed

## 1. Clone the repo

```bash
ssh root@your-vps
cd /opt
git clone <your-repo-url> hull-service
cd hull-service
```

## 2. Set environment variables

Create `/opt/hull-service/.env`:

```bash
# Required: secret key for signing tunnel tokens (generate with: openssl rand -hex 32)
HULL_SECRET_KEY=your-generated-secret-key

# Optional: base URL for tunnels (default: https://hull.openexplorer.xyz)
HULL_BASE_URL=https://hull.openexplorer.xyz

# Optional: SQLite database path (default: /var/lib/hull/tunnels.db)
HULL_DB_PATH=/var/lib/hull/tunnels.db
```

Generate a secret key:
```bash
openssl rand -hex 32
```

## 3. Docker Compose

Create or update `docker-compose.yml`:

```yaml
services:
  hull-service:
    build: .
    ports:
      - "8000:8000"
    env_file:
      - .env
    volumes:
      - hull-data:/var/lib/hull
    restart: unless-stopped

volumes:
  hull-data:
    driver: local
```

## 4. Nginx reverse proxy

Create `/etc/nginx/sites-available/hull.openexplorer.xyz`:

```nginx
server {
    listen 80;
    server_name hull.openexplorer.xyz;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 86400;
    }
}
```

Enable the site:
```bash
ln -s /etc/nginx/sites-available/hull.openexplorer.xyz /etc/nginx/sites-enabled/
nginx -t
systemctl reload nginx
```

## 5. SSL with Certbot

```bash
certbot --nginx -d hull.openexplorer.xyz
```

Or if using standalone:
```bash
certbot certonly --nginx -d hull.openexplorer.xyz
```

## 6. Start the service

```bash
cd /opt/hull-service
docker compose up -d
```

Check logs:
```bash
docker compose logs -f
```

## 7. Verify

```bash
curl https://hull.openexplorer.xyz/health
# Should return: {"status":"ok","service":"hull"}
```

## How Users Connect

### Remote users (tunnel through your service):

1. User installs hull: `pip install hull`
2. User runs: `hull run --remote`
3. Hull connects to your service via WebSocket
4. User gets a URL like: `https://hull.openexplorer.xyz/abc123def456`
5. User adds that URL as a custom connector in Claude Desktop
6. Claude Desktop connects through your service to the user's machine

### Local users (no tunnel):

1. User installs hull: `pip install hull`
2. User adds to Claude Desktop config:
   ```json
   {
     "mcpServers": {
       "hull": {
         "command": "hull",
         "args": ["run", "--local"]
       }
     }
   }
   ```
3. Claude Desktop connects directly to the local hull server

## Architecture

```
Claude Desktop                    Your VPS (hull.openexplorer.xyz)                    User's Machine
      │                                    │                                            │
      │  1. MCP request                    │                                            │
      │  ─────────────────────────────────►│                                            │
      │                                    │  2. Look up tunnel by URL token            │
      │                                    │  3. Verify auth token (if set)             │
      │                                    │  4. Forward via WebSocket                  │
      │                                    │  ─────────────────────────────────────────►│
      │                                    │                                            │
      │                                    │  5. Hull executes command                  │
      │                                    │  ◄─────────────────────────────────────────│
      │  ◄─────────────────────────────────│  6. Response back to Claude                │
```

## Security

- Each tunnel URL has a 32+ character random token
- Tunnels expire after 1 hour (configurable up to 24 hours)
- Optional per-tunnel auth token (extra layer of protection)
- No persistent access — restarting hull creates a new URL
- Database stores token hashes, not plaintext

## Monitoring

Check active tunnels:
```bash
sqlite3 /var/lib/hull/tunnels.db "SELECT * FROM tunnels WHERE active = 1;"
```

Check request counts:
```bash
sqlite3 /var/lib/hull/tunnels.db "SELECT tunnel_id, machine_name, requests_served, expires_at FROM tunnels;"
```

## Troubleshooting

**User can't connect:**
- Check hull is running on their machine: `hull run --remote`
- Check tunnel is active on VPS: `docker compose logs`
- Check Nginx is routing correctly: `curl http://localhost:8000/health`

**WebSocket not working:**
- Verify Nginx has `proxy_set_header Upgrade` and `Connection "upgrade"`
- Check `proxy_read_timeout` is long enough (86400s recommended)

**SSL issues:**
- Run `certbot renew` to refresh certificates
- Check certificate: `openssl s_client -connect hull.openexplorer.xyz:443`
