#!/usr/bin/env bash
set -u

echo "BiddingFlow deployment status"
echo "api_service=$(systemctl is-active biddingflow-api 2>/dev/null || true)"
echo "nginx_service=$(systemctl is-active nginx 2>/dev/null || true)"
echo "auto_deploy_timer=$(systemctl is-active biddingflow-auto-deploy.timer 2>/dev/null || true)"
echo "backend_repo=$(runuser -u ubuntu -- git -C /opt/biddingflow/backend rev-parse HEAD 2>/dev/null || true)"
echo "backend_deployed=$(cat /var/lib/biddingflow/backend.sha 2>/dev/null || true)"
echo "frontend_repo=$(runuser -u ubuntu -- git -C /opt/biddingflow/frontend rev-parse HEAD 2>/dev/null || true)"
echo "frontend_deployed=$(cat /var/lib/biddingflow/frontend.sha 2>/dev/null || true)"
echo "frontend_release=$(readlink -f /var/www/biddingflow/current 2>/dev/null || true)"

if [[ -r /var/lib/biddingflow/deployment-status.json ]]; then
  echo
  echo "Last deployment"
  cat /var/lib/biddingflow/deployment-status.json
fi

echo
echo "API health"
curl --fail --silent --show-error http://127.0.0.1:8000/api/health || true
echo
