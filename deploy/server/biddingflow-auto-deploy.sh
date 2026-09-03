#!/usr/bin/env bash
set -Eeuo pipefail

backend_dir=/opt/biddingflow/backend
frontend_dir=/opt/biddingflow/frontend
frontend_releases=/opt/biddingflow/releases/frontend
frontend_current=/var/www/biddingflow/current
state_dir=/var/lib/biddingflow
log_dir=/var/log/biddingflow
config_file=/etc/biddingflow/deploy.conf

BACKEND_BRANCH=main
FRONTEND_BRANCH=feat/adopt-procurement-mock

if [[ -r "$config_file" ]]; then
  # shellcheck disable=SC1090
  source "$config_file"
fi

install -d -o ubuntu -g ubuntu "$frontend_releases"
install -d -o root -g ubuntu -m 775 "$state_dir" "$log_dir"
touch "$log_dir/deploy.log"
chown root:ubuntu "$log_dir/deploy.log"
chmod 664 "$log_dir/deploy.log"

exec 9>"$state_dir/deploy.lock"
if ! flock -n 9; then
  echo "A BiddingFlow deployment is already running."
  exit 0
fi

exec > >(tee -a "$log_dir/deploy.log") 2>&1

started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "[$started_at] deployment check started"

as_ubuntu() {
  runuser -u ubuntu -- "$@"
}

write_status() {
  local result="$1"
  local message="$2"
  local backend_sha frontend_sha finished_at

  backend_sha="$(cat "$state_dir/backend.sha" 2>/dev/null || true)"
  frontend_sha="$(cat "$state_dir/frontend.sha" 2>/dev/null || true)"
  finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  cat > "$state_dir/deployment-status.json.tmp" <<EOF
{
  "result": "$result",
  "message": "$message",
  "started_at": "$started_at",
  "finished_at": "$finished_at",
  "backend_commit": "$backend_sha",
  "frontend_commit": "$frontend_sha"
}
EOF
  mv "$state_dir/deployment-status.json.tmp" "$state_dir/deployment-status.json"
  chmod 644 "$state_dir/deployment-status.json"
  cp "$state_dir/deployment-status.json" /var/www/biddingflow/deployment-status.json
}

on_error() {
  local line="$1"
  write_status failed "deployment failed at line $line"
  echo "Deployment failed at line $line"
}
trap 'on_error "$LINENO"' ERR

deploy_backend() {
  local remote_sha deployed_sha previous_sha

  as_ubuntu git -C "$backend_dir" fetch --quiet origin "$BACKEND_BRANCH"
  remote_sha="$(git -C "$backend_dir" rev-parse "origin/$BACKEND_BRANCH")"
  deployed_sha="$(cat "$state_dir/backend.sha" 2>/dev/null || true)"

  if [[ "$remote_sha" == "$deployed_sha" ]]; then
    echo "Backend is current: $remote_sha"
    return
  fi

  previous_sha="$(git -C "$backend_dir" rev-parse HEAD)"
  echo "Deploying backend $previous_sha -> $remote_sha"

  as_ubuntu git -C "$backend_dir" checkout --quiet "$BACKEND_BRANCH"
  as_ubuntu git -C "$backend_dir" merge --ff-only "$remote_sha"

  "$backend_dir/.venv/bin/python" -m pip install --quiet \
    --index-url https://download.pytorch.org/whl/cpu torch
  "$backend_dir/.venv/bin/python" -m pip install --quiet \
    -r "$backend_dir/requirements.txt" pytest
  "$backend_dir/.venv/bin/python" -m compileall -q \
    "$backend_dir/main.py" \
    "$backend_dir/auth_service" \
    "$backend_dir/backend_logic2" \
    "$backend_dir/procurement_db" \
    "$backend_dir/scripts"
  "$backend_dir/.venv/bin/python" -m pytest -q "$backend_dir/tests"
  "$backend_dir/.venv/bin/python" -m procurement_db.migrate

  if ! systemctl restart biddingflow-api; then
    as_ubuntu git -C "$backend_dir" checkout --quiet "$previous_sha"
    systemctl restart biddingflow-api || true
    return 1
  fi

  if ! curl --fail --silent --show-error --retry 10 --retry-delay 2 \
    http://127.0.0.1:8000/api/health >/dev/null; then
    as_ubuntu git -C "$backend_dir" checkout --quiet "$previous_sha"
    systemctl restart biddingflow-api || true
    return 1
  fi

  printf '%s\n' "$remote_sha" > "$state_dir/backend.sha"
  echo "Backend deployment succeeded: $remote_sha"
}

deploy_frontend() {
  local remote_sha deployed_sha release_dir temporary_dir previous_target

  as_ubuntu git -C "$frontend_dir" fetch --quiet origin "$FRONTEND_BRANCH"
  remote_sha="$(git -C "$frontend_dir" rev-parse "origin/$FRONTEND_BRANCH")"
  deployed_sha="$(cat "$state_dir/frontend.sha" 2>/dev/null || true)"

  if [[ "$remote_sha" == "$deployed_sha" ]]; then
    echo "Frontend is current: $remote_sha"
    return
  fi

  echo "Deploying frontend $deployed_sha -> $remote_sha"
  as_ubuntu git -C "$frontend_dir" checkout --quiet "$FRONTEND_BRANCH"
  as_ubuntu git -C "$frontend_dir" merge --ff-only "$remote_sha"

  release_dir="$frontend_releases/$remote_sha"
  temporary_dir="$frontend_releases/.tmp-$remote_sha"

  if [[ ! -s "$release_dir/index.html" ]]; then
    test ! -e "$temporary_dir"
    install -d -o ubuntu -g ubuntu "$temporary_dir"

    as_ubuntu docker run --rm \
      --user "$(id -u ubuntu):$(id -g ubuntu)" \
      -e HOME=/tmp \
      -e npm_config_cache=/tmp/.npm \
      -e VITE_PROCUREMENT_DATA_MODE=api \
      -v "$frontend_dir:/app" \
      -v "$temporary_dir:/release" \
      -w /app \
      node:22-alpine \
      sh -c 'npm ci && npm run lint && npm run build -- --outDir /release'

    test -s "$temporary_dir/index.html"
    mv "$temporary_dir" "$release_dir"
  fi

  previous_target="$(readlink -f "$frontend_current" 2>/dev/null || true)"
  ln -sfn "$release_dir" "$frontend_current"

  if ! nginx -t || ! systemctl reload nginx; then
    if [[ -n "$previous_target" ]]; then
      ln -sfn "$previous_target" "$frontend_current"
      systemctl reload nginx || true
    fi
    return 1
  fi

  if ! curl --fail --silent --show-error http://127.0.0.1:8081/ >/dev/null; then
    if [[ -n "$previous_target" ]]; then
      ln -sfn "$previous_target" "$frontend_current"
      systemctl reload nginx || true
    fi
    return 1
  fi

  printf '%s\n' "$remote_sha" > "$state_dir/frontend.sha"
  echo "Frontend deployment succeeded: $remote_sha"
}

deploy_backend
deploy_frontend
write_status success "deployment completed"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] deployment check completed"
