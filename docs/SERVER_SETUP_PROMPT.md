# Prompt for Claude Code on the Linux server

Copy everything in the block below into Claude Code running on the server. Replace the three
`<...>` values first — Claude will ask if you leave them, but filling them in saves a round trip.

---

```
You are setting up the Cinagi WhatsApp platform on this Linux server for production. Work
carefully, explain what you are about to do before anything destructive, and stop and ask me
if something looks wrong rather than guessing.

WHAT THIS IS
A Django 6 / PostgreSQL / Redis / Celery / Channels application that runs a multi-workspace
WhatsApp Business inbox. Each workspace is one WhatsApp number with its own credentials, agents,
templates and chats. It ships with Docker Compose, an nginx front end, and a deploy script.

Repository: https://github.com/arnostrauss19870605/cinagiwhatsapp  (branch: main)
Read docs/DEPLOY.md, README.md and CLAUDE.md in the repo before you start — they contain the
deployment steps, the environment variables and the house rules. Follow them.

VALUES I AM GIVING YOU
  Domain:            <wa.yourdomain.co.za>
  My WhatsApp number for testing (international format, no +):  <27821234567>
  TLS approach:      <cloudflare-tunnel | certbot | already terminated upstream>

GOAL
1. The app running on this server behind HTTPS, reachable at the domain above.
2. Every future release is one command on this server: `cd /opt/cinagi/app && ./scripts/deploy.sh`
3. Nothing secret is ever committed or pushed. `.env` lives only on this machine.

DO THIS

1. Survey first. Report OS and version, available disk and RAM, whether docker and the docker
   compose plugin are installed and at what versions, whether git is installed, whether ports 80
   and 443 are free, and whether anything else is already listening on 5432 or 6379. Do not
   install anything until you have shown me this and I have agreed.

2. Install only what is missing: docker engine, docker compose plugin, git, curl. Add my user to
   the docker group. Do not run the application as root.

3. Clone the repository to /opt/cinagi/app, owned by my user, on branch main.
   If the repository is private, generate a read-only deploy key on this server
   (`ssh-keygen -t ed25519 -C "cinagi-wa-<hostname>" -f ~/.ssh/cinagi_deploy -N ""`), print the
   public key, and pause so I can add it under the repo's Settings > Deploy keys. Do not ask me
   for a personal access token, and never write a token into a git remote URL.

4. Create /opt/cinagi/app/.env from .env.example. Generate DJANGO_SECRET_KEY and
   FIELD_ENCRYPTION_KEY on this machine (Django's get_random_secret_key and Fernet.generate_key).
   Set at minimum:
     DJANGO_DEBUG=False
     DJANGO_ALLOWED_HOSTS=<the domain>
     PUBLIC_BASE_URL=https://<the domain>
     CSRF_TRUSTED_ORIGINS=https://<the domain>
     DB_ENGINE=postgres, DB_HOST=postgres, DB_NAME=cinagi_wa, DB_USER=cinagi,
       DB_PASSWORD=<generate a long random one>
     USE_REDIS=True, REDIS_URL=redis://redis:6379
     OUTBOUND_COMMS_MODE=allowlist
     OUTBOUND_ALLOWLIST=<my test number>
   chmod 600 the file. Confirm .env is git-ignored (it is, but check).
   Then print FIELD_ENCRYPTION_KEY once, clearly labelled, and tell me to store it somewhere
   separate from the database backups — it decrypts every workspace's WhatsApp token, and a
   database backup without it cannot be restored into a working system.

5. Bring the stack up with docker-compose.prod.yml. Then:
     - create a superuser (ask me for the username and email; I will type the password)
     - run `python manage.py seed_schedules`
     - confirm `curl -s localhost/healthz` returns status ok with database ok and cache ok
     - show me `docker compose -f docker-compose.prod.yml ps` and confirm every service is healthy

6. Set up TLS using the approach I chose above, so that https://<domain>/healthz answers from
   outside the machine. Django already trusts X-Forwarded-Proto. If you chose or I chose
   Cloudflare Tunnel, install cloudflared and walk me through the browser authorisation step.

7. Verify the deploy path works: make a trivial change upstream is not needed — just run
   `./scripts/deploy.sh` once and show me that it fast-forwards, rebuilds, recreates nginx, and
   passes the health check. Explain in one line what I run after every future push.

8. Set up a nightly database backup to /var/backups (pg_dump piped through gzip, 30 day
   retention) and tell me plainly that these backups leave nothing off this machine yet, so I
   still need to copy them somewhere else.

9. Basic hardening: ufw allowing only SSH and 80/443 (or only SSH if a tunnel is terminating
   traffic), unattended security updates, and confirm postgres and redis are not exposed on the
   public interface.

10. Finish with a short handover in plain language:
    - the exact Callback URL to paste into Meta (https://<domain>/wa/webhook/) and where to find
      the verify token (it is shown in the app on each number's check screen)
    - the one command to deploy in future
    - where .env lives and what must never be committed
    - what is still on me: Meta number setup, connecting the number in the app, loading templates,
      and switching OUTBOUND_COMMS_MODE to live once a real test message has arrived on my phone

RULES
- Never commit, push, or otherwise put .env, tokens, keys or passwords into git. Check before any
  git command that would create a commit.
- Do not switch OUTBOUND_COMMS_MODE to live. That is my decision, after a successful test.
- Do not run migrations by hand against a database you have not backed up.
- Prefer the repo's own scripts and compose files over improvising your own.
- If a step fails, show me the actual error and your reading of it before trying a fix.
```
