#!/usr/bin/env bash
set -euo pipefail

# Keep the campaign client present across softUE container restarts and reset
# local media state after a fatal encoder/capture failure. Campaign identity
# remains in the mounted log directory passed to softue-client.sh.
container="${SOFTUE_CONTAINER:-oai-nr-ue}"
retry_s="${SOFTUE_CLIENT_RETRY_S:-2}"
lock="${SOFTUE_CLIENT_LOCK:-/tmp/aetheredge-softue-client.lock}"

exec 9>"$lock"
if ! flock -n 9; then
  echo "A softUE campaign client supervisor is already running" >&2
  exit 1
fi

stopping=false
trap 'stopping=true' INT TERM

while ! $stopping; do
  if [[ "$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null || true)" != true ]]; then
    sleep "$retry_s"
    continue
  fi

  set +e
  "$(dirname "${BASH_SOURCE[0]}")/softue-client.sh" "$@"
  status=$?
  set -e

  $stopping && break
  echo "softUE campaign client exited with status $status; retrying in ${retry_s}s" >&2
  sleep "$retry_s"
done
