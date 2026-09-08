# BiddingFlow AWS deployment automation

The production-like deployment keeps the backend and frontend repositories in
separate directories and serves the frontend build through Nginx.

## Server paths

- Backend: `/opt/biddingflow/backend`
- Frontend source: `/opt/biddingflow/frontend`
- Frontend releases: `/opt/biddingflow/releases/frontend/<commit>`
- Active frontend symlink: `/var/www/biddingflow/current`
- Backend secrets: `/etc/biddingflow/backend.env`
- Deployment config: `/etc/biddingflow/deploy.conf`
- Deployment state: `/var/lib/biddingflow/deployment-status.json`
- Deployment log: `/var/log/biddingflow/deploy.log`

The production defaults track the `main` branch of both the backend and
frontend repositories. `/etc/biddingflow/deploy.conf` can override either
branch temporarily when a staged deployment is required.
The deploy script fetches the selected branch with an explicit refspec, so a
repository originally cloned with `--single-branch` can also change its tracked
branch safely.

## Commands

```bash
sudo systemctl status biddingflow-api
sudo systemctl status biddingflow-auto-deploy.timer
sudo journalctl -u biddingflow-api -f
sudo journalctl -u biddingflow-auto-deploy.service -f
sudo tail -f /var/log/biddingflow/deploy.log
sudo biddingflow-status
```

The timer checks the configured Git branches every minute. It performs work
only when a remote commit differs from the last successfully deployed commit.
Backend tests and migrations run before restart. Frontend builds are placed in
a commit-specific release directory and the active symlink changes only after
lint and build succeed.

## GitHub Actions immediate deployment

`Backend CD` runs only after `Backend CI` succeeds for a push to `main`, or
when it is started manually. The workflow sends only a component name and the
tested 40-character commit SHA through a timestamped HMAC request over HTTPS.
The server rejects expired, incorrectly signed, or malformed requests, and
deployment also fails if `main` moved before the server fetched it.

Server-side files:

- `/usr/local/sbin/biddingflow-github-ssh-command`: forced SSH command
- `/usr/local/sbin/biddingflow-github-dispatch`: validates component and SHA
- `/usr/local/lib/biddingflow/deploy-trigger.py`: loopback-only HMAC endpoint
- `/etc/biddingflow/deploy-trigger.env`: root-managed deployment token
- `biddingflow-deploy-trigger.service`: local trigger service
- `/etc/sudoers.d/biddingflow-github-deploy`: permits only the dispatcher
- `/var/lib/biddingflow-deploy/.ssh/authorized_keys`: restricted Actions key

GitHub repository configuration:

1. Create the `production` environment and restrict its deployment branch to
   `main`.
2. Add the shared HMAC token as the environment or repository secret
   `BIDDINGFLOW_DEPLOY_TOKEN`.
3. Keep the timer enabled until both backend and frontend workflows have been
   verified. It can then remain as reconciliation fallback or be disabled.
