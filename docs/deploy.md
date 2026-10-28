# Deploying SplitSync

SplitSync runs on the same EC2 host as Hopper and Sift, as its own Compose project
(`splitsync`) on Hopper's Docker network, reached through Hopper's Caddy at
**`http://<host>/splitsync/`**. It uses Hopper's Postgres (database `splitsync`, its own role) and
Hopper's Redis (db 2). Nothing is published on the host: Caddy is the only way in.

| Process | Memory limit | Notes |
|---|---|---|
| splitsync-app | 400 MB | Uvicorn, 2 workers; API, WebSockets and pages |
| splitsync-migrate | (one-off) | `alembic upgrade head` |

## One-time setup on the host

```bash
sudo mkdir -p /opt/splitsync && sudo chown ubuntu: /opt/splitsync
git clone https://github.com/Srinu-design/splitsync /opt/splitsync/src
# A role and database in Hopper's Postgres; the password is generated on the host, never shown:
pw=$(openssl rand -hex 24)
docker exec -i hopper-postgres-1 psql -U hopper -d hopper -v ON_ERROR_STOP=1 \
  -c "CREATE ROLE splitsync LOGIN PASSWORD '$pw'" -c "CREATE DATABASE splitsync OWNER splitsync"
# /opt/splitsync/.env (chmod 600): DATABASE_URL=postgresql+asyncpg://splitsync:$pw@postgres:5432/splitsync,
# REDIS_URL=redis://redis:6379/2, ALLOWED_ORIGINS=http://<host>, DB_POOL_SIZE=3, DB_MAX_OVERFLOW=2
```

Hopper's side: its Caddyfile routes `/splitsync/` to `splitsync-app:8000` (`handle_path` strips the
prefix; the pages use relative URLs, so they work under it).

## A release

`/opt/splitsync/src/deploy/deploy.sh <git ref>` (exit codes and steps in the script's header):

1. build `splitsync:<sha>` from the checked-out ref;
2. run the migrations with it (backward compatible, so the live release keeps working);
3. replace splitsync-app and wait until Docker reports it healthy (`/readyz`);
4. **smoke test**, `python -m app.smoke` in the new container: a fresh demo room must show the
   seed's verified numbers (version 9, 10 IOUs, greedy 4, optimal 3, zero drift).

If 3 or 4 fail, the previous release comes back the same way and is smoke tested too.
`deploy.sh --rollback` and `deploy.sh --status` do what they say. Every step is logged to
`/opt/splitsync/deploy-history.log`.

## Deploying from CI

`.github/workflows/deploy.yml` deploys every push to main once `ci` is green, and on demand any
sha or `--rollback` (Actions > deploy > Run workflow). It runs `ssh -- splitsync <sha>` with a key
that can do one thing: its line in the host's `~/.ssh/authorized_keys` is

```
restrict,command="/opt/splitsync/src/deploy/deploy.sh --from-ssh" ssh-ed25519 ... splitsync-github-deploy
```

so whatever the key sends arrives as `SSH_ORIGINAL_COMMAND`, and `deploy.sh --from-ssh` accepts
only a full commit sha, `--rollback` or `--status` (`tests/unit/test_deploy_script.py`).

One-time setup (the private key goes from the owner's machine to GitHub, never through tooling):

```bash
ssh-keygen -t ed25519 -f ~/.ssh/splitsync-deploy -N "" -C splitsync-github-deploy
gh secret set EC2_SSH_KEY -R Srinu-design/splitsync < ~/.ssh/splitsync-deploy
ssh-keygen -F <host> | grep -v '^#' | gh secret set EC2_KNOWN_HOSTS -R Srinu-design/splitsync
gh variable set EC2_HOST -R Srinu-design/splitsync -b <host>
gh variable set PUBLIC_URL -R Srinu-design/splitsync -b http://<host>/splitsync/
```
