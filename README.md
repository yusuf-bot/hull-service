# Hull Service

Tunnel service for [hull](https://github.com/yourname/hull) MCP servers.
Generates temporary public URLs so remote AI agents can connect to any machine.

## Quick Start

```bash
# Clone and install
git clone https://github.com/yourname/hull-service
cd hull-service
pip install -r requirements.txt

# Run (dev)
uvicorn app:app --reload

# Run (production)
uvicorn app:app --host 0.0.0.0 --port 8000
```

## How It Works

1. User runs `hull run --remote` on their machine
2. Hull connects to this service via WebSocket
3. Service generates a temporary public URL
4. User adds URL to Claude Desktop as a custom connector
5. Agent requests flow: Claude → public URL → service → WebSocket → hull → response

## API

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/tunnel/create` | Create a new tunnel |
| GET | `/api/tunnel/{id}/status` | Check tunnel status |
| DELETE | `/api/tunnel/{id}` | Close tunnel |
| WS | `/ws/{id}` | WebSocket for hull servers |
| ANY | `/{url_token}/*` | Proxy to hull server |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `HULL_DB_PATH` | `/var/lib/hull/tunnels.db` | SQLite database path |
| `HULL_SECRET_KEY` | (random) | Secret key for signing |
| `HULL_BASE_URL` | `https://hull.openexplorer.xyz` | Public base URL |
| `HULL_PORT` | `8000` | Server port |

## Authentication

- **With emails**: When `allowed_emails` is set, OAuth flow is required. Claude Desktop will show a login page.
- **Without emails**: Tunnel is open. Anyone with the URL can access it (no auth required).

For quick testing, create a tunnel without `--email`:
```bash
# On the hull machine
hull run --remote  # No --email = open tunnel

# Or with Python directly
python -c "from hull.cli import cli; cli()" run --remote
```

## Production Deploy (Ubuntu)

```bash
# Install system deps
sudo apt update && sudo apt install -y nginx certbot python3-certbot-nginx

# Get SSL cert
sudo certbot certonly --standalone -d hull.openexplorer.xyz

# Copy nginx config
sudo cp nginx.conf /etc/nginx/sites-available/hull
sudo ln -s /etc/nginx/sites-available/hull /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# Set up systemd service
sudo cp hull.service /etc/systemd/system/
sudo systemctl enable hull
sudo systemctl start hull
```

## Free Tier Limits

- 1 tunnel per machine
- 1 hour TTL (renewable)
- 10MB max request/response
- 100 requests/minute rate limit
