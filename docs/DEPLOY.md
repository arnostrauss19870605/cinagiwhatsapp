# Deploying to the Linux server

Everything below assumes a fresh Ubuntu/Debian host with Docker and the Docker Compose plugin
installed, and a DNS name pointing at it.

---

## 1. Get the code onto the server

```bash
sudo mkdir -p /opt/cinagi && sudo chown $USER:$USER /opt/cinagi
cd /opt/cinagi
git clone https://github.com/arnostrauss19870605/cinagiwhatsapp.git app
cd app
```

For a private repository, use a deploy key (read-only, per-server) rather than a personal token:

```bash
ssh-keygen -t ed25519 -C "cinagi-wa-server" -f ~/.ssh/cinagi_deploy -N ""
cat ~/.ssh/cinagi_deploy.pub      # add this in GitHub: repo -> Settings -> Deploy keys
git clone git@github.com:arnostrauss19870605/cinagiwhatsapp.git app
```

---

## 2. Create the server's `.env`

`.env` is **never** in the repository. Create it on the server and keep it out of backups that leave
the machine.

```bash
cp .env.example .env
```

Generate two keys and paste them in:

```bash
docker run --rm python:3.13-slim sh -c "pip -q install django cryptography && python - <<'PY'
from django.core.management.utils import get_random_secret_key
from cryptography.fernet import Fernet
print('DJANGO_SECRET_KEY=' + get_random_secret_key())
print('FIELD_ENCRYPTION_KEY=' + Fernet.generate_key().decode())
PY"
```

Then set at least:

```ini
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=wa.yourdomain.co.za
PUBLIC_BASE_URL=https://wa.yourdomain.co.za
CSRF_TRUSTED_ORIGINS=https://wa.yourdomain.co.za

DB_ENGINE=postgres
DB_NAME=cinagi_wa
DB_USER=cinagi
DB_PASSWORD=<a long random password>
DB_HOST=postgres
DB_PORT=5432

USE_REDIS=True
REDIS_URL=redis://redis:6379

# Start here. Switch to live only once you have sent yourself a test message.
OUTBOUND_COMMS_MODE=allowlist
OUTBOUND_ALLOWLIST=27821234567
```

> **`FIELD_ENCRYPTION_KEY` decrypts every workspace's WhatsApp token.** Back it up somewhere safe
> and separate from the database. Losing it means re-entering credentials for every number.

---

## 3. Start it

```bash
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml exec web python manage.py createsuperuser
docker compose -f docker-compose.prod.yml exec web python manage.py seed_schedules
docker compose -f docker-compose.prod.yml logs -f web
```

`migrate` and `collectstatic` run automatically on every container start.

Check it is alive:

```bash
curl -s 127.0.0.1:8080/healthz
# {"status": "ok", "database": "ok", "cache": "ok"}
```

The stack's nginx binds to **127.0.0.1:8080**, not port 80. The host's own nginx
terminates TLS on 80/443 and proxies to it, so nothing in this compose file is
reachable from the internet directly.

---

## 4. TLS

The stack expects something in front of it on 80/443. Pick one.

**Host nginx + certbot (current setup):** a server block on the host holds the certificate and
proxies to the container:

```nginx
server {
    listen 443 ssl;
    server_name brokers.cinagi.co.za;
    # ssl_certificate ... managed by certbot

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;   # must be https here
    }

    location /ws/ {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 3600s;
    }
}
```

The `/ws/` block is not optional - without it the live inbox falls back to polling.

**Cloudflare Tunnel:** point the tunnel at `http://localhost:8080` instead; no host nginx needed.

Either way, the container nginx passes an inbound `X-Forwarded-Proto` straight through rather than
overwriting it, so Django sees `https` and secure cookies behave. Leave `SECURE_SSL_REDIRECT=False`
in `.env` - the host nginx does the http-to-https redirect, and doing it in both places loops.

---

## 5. Point WhatsApp at the server

In Meta's app dashboard, **WhatsApp -> Configuration**:

- Callback URL: `https://wa.yourdomain.co.za/wa/webhook/`
- Verify token: shown on the number's check screen inside the app
- Subscribe to the **messages** field

Then in the app: connect the number, press **Check connection**, send yourself a test message, and
load the approved templates.

---

## 6. Updating

```bash
cd /opt/cinagi/app
git pull
docker compose -f docker-compose.prod.yml up -d --build

# nginx caches the app container's IP at startup; recreate it after a rebuild
docker compose -f docker-compose.prod.yml up -d --force-recreate nginx
```

Or just `./scripts/deploy.sh`, which does the pull, the rebuild, the nginx recreate and the health
check in one go. If you republish nginx on a different port, tell it:
`HEALTH_URL=http://127.0.0.1:<port>/healthz ./scripts/deploy.sh`

---

## 7. Backups

The database holds every conversation and the encrypted credentials.

```bash
# /etc/cron.daily/cinagi-backup
docker compose -f /opt/cinagi/app/docker-compose.prod.yml exec -T postgres \
  pg_dump -U cinagi cinagi_wa | gzip > /var/backups/cinagi_wa_$(date +%F).sql.gz
find /var/backups -name 'cinagi_wa_*.sql.gz' -mtime +30 -delete
```

Copy those off the machine, and store `FIELD_ENCRYPTION_KEY` separately - a database backup without
the key cannot be used, which is the point.

Media (customer attachments) lives in the `media_files` Docker volume; back that up too.

---

## 8. Going live

1. Connection check passes and templates are loaded.
2. Send yourself a real message with `OUTBOUND_COMMS_MODE=allowlist`.
3. Set `OUTBOUND_COMMS_MODE=live` and restart: `docker compose -f docker-compose.prod.yml up -d`.
4. The amber banner at the top of the app disappears - that is your confirmation that real customers
   can now be messaged.
