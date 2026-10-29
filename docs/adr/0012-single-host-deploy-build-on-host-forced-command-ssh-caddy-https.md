# ADR-0012: Single-host deploy: build on host, forced-command SSH key, smoke test, auto rollback, Caddy HTTPS on sslip.io

Status: Accepted · Date: 2026-10-29

## Context

Deploying portfolio projects often introduces fragile external dependencies: container registries (e.g. GitHub Packages / GHCR) requiring registry credentials, external web servers with manual certificate renewal, and deployment pipelines that lack automated rollback when a subtle logic bug breaks ledger arithmetic.

SplitSync runs on a single AWS EC2 instance (`portfolio-apps`, `t3.small`: 2 vCPU, 2 GB RAM + 2 GB swap, 20 GB gp3 storage in `ap-southeast-2`, Elastic IP `3.25.14.19`). The host shares base infrastructure in `deploy/portfolio/` (Caddy 2, PostgreSQL 16 with pgvector 0.8.7, and Redis 8) with future applications.

## Decision

The deployment pipeline is fully self-contained on the host, executed by [`deploy/deploy.sh`](../../deploy/deploy.sh):

- **Image build directly on host:**
  The host maintains a git clone in `/opt/splitsync/src`. Deployments fetch the target commit SHA and run `docker build -t splitsync:$sha .` directly on the instance, eliminating third-party container registry fees, rate limits, and external network transfer overhead.
- **Caddy 2 for automated HTTPS and HTTP/2:**
  Caddy 2 (`caddy:2.11.6`) serves the public IP using `3-25-14-19.sslip.io`. Caddy automatically acquires and renews free Let's Encrypt TLS certificates. It serves HTTP/2, multiplexing concurrent browser requests over a single TCP connection (essential for the chaos demo, where HTTP/1.1 would queue requests past the 6-connection browser limit).
- **Forced-command SSH deployment key:**
  GitHub Actions triggers deploys via SSH using a dedicated Ed25519 key restricted via `~/.ssh/authorized_keys`:
  ```
  restrict,command="/opt/splitsync/src/deploy/deploy.sh --from-ssh" ssh-ed25519 ... splitsync-github-deploy
  ```
  The script accepts only a 40-character hex commit SHA, `--rollback`, or `--status`. Any attempt to execute arbitrary commands or interactive shells is immediately rejected.
- **The release lifecycle:**
  1. `git fetch origin` and checkout the commit SHA.
  2. `docker build` the new release image.
  3. Run database migrations via one-off container: `docker compose run --rm splitsync-migrate alembic upgrade head`.
  4. Replace `splitsync-app` and wait up to 120 seconds for container healthcheck (`/readyz`).
  5. **Execute smoke test:** Run [`python -m app.smoke`](../../app/smoke.py) inside the container. The smoke test creates a fresh demo room and verifies the exact mathematical invariants: version 9, 10 pairwise IOUs, 4 greedy transfers, 3 optimal transfers, and ₹0 drift.
  6. **Automated rollback:** If the health check or smoke test fails, `deploy.sh` checks out the previous release, starts its image again, and verifies it with the smoke test. Every step logs to `/opt/splitsync/deploy-history.log`.

## Alternatives considered

- **Pushing images to GitHub Container Registry (GHCR).** Adds external token management, external bandwidth transfer delays, and failure modes when registry rate limits or authentication tokens expire.
- **Nginx with Certbot.** Requires separate cron jobs for certificate renewals, manual config reloading, and complex HTTP/2 configuration compared to Caddy's single Caddyfile.
- **Plain HTTP on the bare IP.** Works, but browsers speak HTTP/2 only over TLS, so the chaos demo's 10 requests would queue on 6 HTTP/1.1 connections. `sslip.io` with Caddy gives trusted HTTPS without buying a domain.

## Consequences

- `deploy.sh` takes about **18 seconds** from build to a smoke-tested release (the whole deploy job: 29 s, after about 2 minutes of CI).
- If a release boots cleanly but contains an arithmetic bug in ledger calculation, the smoke test catches it and rolls back the deployment automatically with zero human intervention.
- The deploy key cannot be abused to compromise the host operating system.
- Validated in [`tests/unit/test_deploy_script.py`](../../tests/unit/test_deploy_script.py) and [`tests/integration/test_smoke.py`](../../tests/integration/test_smoke.py).

## When we would revisit

If deployment frequency or multi-node clustering requires an orchestrated container registry and blue/green load balancers across multiple availability zones.
