#!/bin/sh
# Launch hueman from the project's virtualenv.
cd "$(dirname "$0")" && exec .venv/bin/python -m hueman "$@"
