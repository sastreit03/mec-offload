#!/usr/bin/env bash
set -euo pipefail

# Keep the historical entry point synchronized with the maintained launcher.
exec "$(dirname "$0")/scripts/containers.sh" "$@"
