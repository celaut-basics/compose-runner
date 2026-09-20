#!/bin/sh
# Every offline test, with the service's own modules importable.
#
#     sh tests/run.sh
#
# Python 3 and nothing else -- no pytest, no venv, no network, no Docker. That is
# deliberate: these are the tests that should run on a workstation before a build, so
# their dependency list is the same as the service's own.
#
# The container-level tests are separate and need a built image and a privileged
# container, because dockerd cannot start without one:
#     sh tests/test_image.sh

set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)

# `service/` and `.service/` on the path, because that is where the modules live in the
# image too -- `/service/supervisor.py` imports `config`, not `service.config`, and a
# test layout that needed a package would be testing a different import graph than the
# one that ships. `.service/` is there for `preflight.py`, which runs during the build.
PYTHONPATH="$ROOT/service:$ROOT/.service" export PYTHONPATH

PYTHON=${PYTHON:-python3}

echo "# python:  $($PYTHON --version 2>&1)"
echo "# modules: $ROOT/service, $ROOT/.service"
echo

cd "$ROOT/tests"
exec "$PYTHON" -m unittest discover -s "$ROOT/tests" -p 'test_*.py' -v
