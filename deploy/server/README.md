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
