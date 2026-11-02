#!/usr/bin/env bash
# bootstrap-pg.sh — download and extract the embedded PostgreSQL 16.2 + pgvector.
#
# This is only needed once, on a machine that does not already have PostgreSQL
# installed and does not use the docker-compose stack. It fetches the
# pip-installable `pgserver` wheel (which bundles PostgreSQL 16.2 and the
# pgvector extension), extracts the binaries into $PG_HOME, starts the server,
# and creates the application database + role.
#
# Usage:
#   scripts/bootstrap-pg.sh
#
# Environment:
#   PG_HOME         install directory  (default: ./.embedded-pg)
#   PG_PORT         listen port        (default: 5433)
#   PG_SOCKET_DIR   unix socket dir    (default: /tmp)
#
# It is safe to run repeatedly (idempotent).

set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PG_HOME="${PG_HOME:-$ROOT_DIR/.embedded-pg}"
export PG_DATA="$PG_HOME/data"
PG_PORT="${PG_PORT:-5433}"
PG_SOCKET_DIR="${PG_SOCKET_DIR:-/tmp}"

# The wheel's *package* version is 0.1.4; the bundled PostgreSQL is 16.2 and the
# bundled pgvector extension is 0.6.2 (used only for the messages below).
PG_PACKAGE_VERSION="0.1.4"
PG_SERVER_VERSION="16.2"
PGVECTOR_VERSION="0.6.2"
WHEEL_NAME="pgserver-${PG_PACKAGE_VERSION}-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"
WHEEL_URL="https://files.pythonhosted.org/packages/cf/f1/c9d717f66d2e4a27801577e1ae233c25aa88db875c586ac3ebe7d73b6b75/${WHEEL_NAME}"
# sha256 of that wheel, looked up from https://pypi.org/pypi/pgserver/0.1.4/json
WHEEL_SHA256="1a5d07c61d51f2abfef4ef61e2ef5cd014b994f7e09de8d3c140d2cf370e84a8"

BIN="$PG_HOME/pginstall/bin"
LOG="$PG_HOME/postgres.log"

# Nothing to do if the cluster already exists.
if [ -f "$BIN/postgres" ]; then
  echo "Embedded PostgreSQL already present at $PG_HOME"
  exit 0
fi

mkdir -p "$PG_HOME"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "Downloading ${WHEEL_NAME} ..."
curl -fsSL -o "$TMP/pgserver.whl" "$WHEEL_URL"

echo "Verifying download (sha256) ..."
echo "$WHEEL_SHA256  $TMP/pgserver.whl" > "$TMP/pgserver.whl.sha256"
sha256sum -c "$TMP/pgserver.whl.sha256"

echo "Extracting PostgreSQL $PG_SERVER_VERSION + pgvector $PGVECTOR_VERSION ..."
unzip -q "$TMP/pgserver.whl" -d "$TMP"
mv "$TMP/pgserver/pginstall" "$PG_HOME/pginstall"
# Copy the bundled .so libraries so the binaries are self-contained.
cp "$TMP/pgserver.libs/"*.so* "$PG_HOME/pginstall/lib/" 2>/dev/null || true
chmod -R u+rwX "$PG_HOME"
echo "Extracted PostgreSQL $PG_SERVER_VERSION + pgvector $PGVECTOR_VERSION"

export LD_LIBRARY_PATH="$PG_HOME/pginstall/lib:${LD_LIBRARY_PATH:-}"

echo "Initializing database cluster ..."
if [ ! -f "$PG_DATA/PG_VERSION" ]; then
  "$BIN/initdb" -D "$PG_DATA" -U memory --auth=trust
fi

echo "Starting PostgreSQL on port $PG_PORT ..."
"$BIN/pg_ctl" -D "$PG_DATA" -l "$LOG" \
  -o "-p $PG_PORT -k $PG_SOCKET_DIR" start
# Wait for readiness.
for _ in $(seq 1 30); do
  if "$BIN/pg_isready" -h "$PG_SOCKET_DIR" -p "$PG_PORT" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
"$BIN/pg_isready" -h "$PG_SOCKET_DIR" -p "$PG_PORT"

echo "Creating application database + role ..."
"$BIN/psql" -h "$PG_SOCKET_DIR" -p "$PG_PORT" -U memory -d postgres -v ON_ERROR_STOP=1 <<'SQL'
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'memory') THEN
    CREATE ROLE memory WITH LOGIN PASSWORD 'memory';
  END IF;
END $$;
SELECT 'CREATE DATABASE memory OWNER memory'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'memory')\gexec
SQL

echo "Enabling pgvector extension ..."
"$BIN/psql" -h "$PG_SOCKET_DIR" -p "$PG_PORT" -U memory -d memory -v ON_ERROR_STOP=1 \
  -c "CREATE EXTENSION IF NOT EXISTS vector;"

echo "Bootstrap complete. Server is running on port $PG_PORT (data: $PG_DATA)."
