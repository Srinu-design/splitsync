# Deploying SplitSync

SplitSync runs on its own EC2 host, `portfolio-apps` (t3.small: 2 vCPU, 2 GB RAM plus 2 GB swap,
20 GB gp3, ap-southeast-2, Elastic IP 3.25.14.19), which TriageLens will share later. It is live
at **https://3-25-14-19.sslip.io/splitsync/**: sslip.io resolves the name to the IP, and Caddy
gets a free Let's Encrypt certificate for it, so the site has HTTPS and HTTP/2 at no cost.

| Compose project | Services | Notes |
|---|---|---|
| `portfolio` (`/opt/portfolio`, from `deploy/portfolio/`) | caddy, postgres (pgvector 0.8.7-pg16), redis 8 | shared; the only published ports are Caddy's 80/443 |
| `splitsync` (`deploy/compose.prod.yaml`) | splitsync-app (400 MB limit), splitsync-migrate (one-off) | on the `portfolio` network |

The instance: Ubuntu 24.04, IMDSv2 required, encrypted EBS, CPU credits in standard mode (no
surplus-credit charges), security group `portfolio-apps` with 80/443 open and 22 open for key-only
SSH (GitHub's deploy runners connect from changing addresses). First boot installs Docker from
Docker's apt repository and a 2 GB swap file.

## One-time setup on the host

```bash
sudo mkdir -p /opt/portfolio /opt/splitsync && sudo chown ubuntu: /opt/portfolio /opt/splitsync
git clone https://github.com/Srinu-design/splitsync /opt/splitsync/src
cp /opt/splitsync/src/deploy/portfolio/{compose.yaml,Caddyfile} /opt/portfolio/
# /opt/portfolio/.env (chmod 600): SITE_ADDRESS=3-25-14-19.sslip.io, POSTGRES_PASSWORD=<random>
cd /opt/portfolio && docker compose up -d
# A role and database for SplitSync; the password is generated on the host, never shown:
pw=$(openssl rand -hex 24)
docker exec -i portfolio-postgres-1 psql -U portfolio -d postgres -v ON_ERROR_STOP=1 \
  -c "CREATE ROLE splitsync LOGIN PASSWORD '$pw'" -c "CREATE DATABASE splitsync OWNER splitsync"
# /opt/splitsync/.env (chmod 600): DATABASE_URL=postgresql+asyncpg://splitsync:$pw@postgres:5432/splitsync,
# REDIS_URL=redis://redis:6379/0, ALLOWED_ORIGINS=https://3-25-14-19.sslip.io, DB_POOL_SIZE=3, DB_MAX_OVERFLOW=2
```

To go back to plain HTTP (for example if the certificate cannot be issued), set
`SITE_ADDRESS=:80`, `ALLOWED_ORIGINS=http://3.25.14.19` and restart both projects.

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
ssh-keyscan -t ed25519 3.25.14.19 | gh secret set EC2_KNOWN_HOSTS -R Srinu-design/splitsync
gh variable set EC2_HOST -R Srinu-design/splitsync -b 3.25.14.19
gh variable set PUBLIC_URL -R Srinu-design/splitsync -b https://3-25-14-19.sslip.io/splitsync/
```

## Stopping and starting

The host can be stopped to save credits (`aws ec2 stop-instances --instance-ids i-07e0843077d6aa110`);
the Elastic IP keeps the address, and every container restarts on boot. The Elastic IP's public
IPv4 address is billed by the hour even while the instance is stopped.
