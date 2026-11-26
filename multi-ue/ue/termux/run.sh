#!/bin/sh
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export PYTHONPATH="$here/../linux/src:$here/../../contracts/python${PYTHONPATH:+:$PYTHONPATH}"
exec python "$here/app.py" "$@"
