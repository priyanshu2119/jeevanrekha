#!/bin/sh
# Container entrypoint: apply schema migrations, then exec the given command.
# Set JR_SKIP_MIGRATIONS=1 on replica/sidecar containers so exactly one
# process migrates at deploy time.
set -e

if [ "${JR_SKIP_MIGRATIONS:-0}" != "1" ]; then
  echo "==> alembic upgrade head"
  alembic upgrade head
fi

exec "$@"
