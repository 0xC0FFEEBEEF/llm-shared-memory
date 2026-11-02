#!/usr/bin/env bash
# start-pg.sh — start the bundled (embedded) PostgreSQL for local development.
#
# PostgreSQL is *not* a service in this environment, so we run a self-contained
# copy of PostgreSQL 16.2 (with the pgvector extension) that is downloaded and
# extracted into ./.embedded-pg. This mirrors what the docker-compose stack does
# in production, but needs no Docker.
#
# Usage:
#   scripts/start-pg.sh            # status (is it accepting connections?)
#   scripts/start-pg.sh start      # start (idempotent)
#   scripts/start-pg.sh stop       # stop it
#
# Environment:
#   PG_DATA      data directory  (default: ./.embedded-pg/data)
#   PG_PORT      listen port     (default: 5433)
#   PG_SOCKET_DIR unix socket dir (default: /tmp)

set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PG_DATA="${PG_DATA:-$ROOT_DIR/.embedded-pg/data}"
PG_PORT="${PG_PORT:-5433}"
PG_SOCKET_DIR="${PG_SOCKET_DIR:-/tmp}"
export LD_LIBRARY_PATH="$ROOT_DIR/.embedded-pg/pginstall/lib:${LD_LIBRARY_PATH:-}"
BIN="$ROOT_DIR/.embedded-pg/pginstall/bin"
LOG="$ROOT_DIR/.embedded-pg/postgres.log"

cmd="${1:-status}"
case "$cmd" in
  status)
    if "$BIN/pg_isready" -h "$PG_SOCKET_DIR" -p "$PG_PORT" >/dev/null 2>&1; then
      echo "PostgreSQL is running on port $PG_PORT (data: $PG_DATA)"
    else
      echo "PostgreSQL is NOT running"
      exit 1
    fi
    ;;
  start)
    if "$BIN/pg_isready" -h "$PG_SOCKET_DIR" -p "$PG_PORT" >/dev/null 2>&1; then
      echo "PostgreSQL already running on port $PG_PORT"
    else
      "$BIN/pg_ctl" -D "$PG_DATA" -l "$LOG" \
        -o "-p $PG_PORT -k $PG_SOCKET_DIR" start
      # wait for readiness
      for i in $(seq 1 30); do
        if "$BIN/pg_isready" -h "$PG_SOCKET_DIR" -p "$PG_PORT" >/dev/null 2>&1; then
          echo "PostgreSQL started on port $PG_PORT (data: $PG_DATA)"
          exit 0
        fi
        sleep 1
      done
      echo "PostgreSQL failed to start; see $LOG" >&2
      tail -20 "$LOG" >&2 || true
      exit 1
    fi
    ;;
  stop)
    "$BIN/pg_ctl" -D "$PG_DATA" stop -m fast 2>/dev/null || true
    echo "PostgreSQL stopped"
    ;;
  *)
    echo "Usage: $0 [status|start|stop]" >&2
    exit 2
    ;;
esac
