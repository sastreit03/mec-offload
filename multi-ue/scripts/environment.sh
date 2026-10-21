# Source this file from bash; no OAI paths are modified.
MEC_APP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="$MEC_APP_ROOT/contracts/python:$MEC_APP_ROOT/mec/src:$MEC_APP_ROOT/ue/linux/src:$MEC_APP_ROOT/../EdgeRIC-v2${PYTHONPATH:+:$PYTHONPATH}"
export PATH="$MEC_APP_ROOT/.venv/bin:$PATH"
