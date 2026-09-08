#!/usr/bin/env bash
set -Eeuo pipefail

original_command="${SSH_ORIGINAL_COMMAND:-}"

if [[ "$original_command" =~ ^deploy[[:space:]]+(backend|frontend)[[:space:]]+([0-9a-f]{40})$ ]]; then
  component="${BASH_REMATCH[1]}"
  commit_sha="${BASH_REMATCH[2]}"
  sudo -n /usr/local/sbin/biddingflow-github-dispatch "$component" "$commit_sha"
  exit $?
fi

echo "Rejected command. Expected: deploy <backend|frontend> <40-character commit SHA>"
exit 64
