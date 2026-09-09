#!/usr/bin/env bash
set -Eeuo pipefail

component="${1:-}"
commit_sha="${2:-}"

case "$component" in
  backend|frontend) ;;
  *)
    echo "Only backend or frontend deployment can be triggered."
    exit 64
    ;;
esac

if [[ ! "$commit_sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "A full 40-character commit SHA is required."
  exit 64
fi

exec /usr/local/sbin/biddingflow-auto-deploy "$component" "$commit_sha"
