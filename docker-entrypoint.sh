#!/bin/sh
set -eu

DATA_DIR="${PKB_DATA_DIR:-/data}"
KB_ROOT="${PKB_KB_ROOT:-$DATA_DIR/notes}"
CACHE_DIR="${PKB_CACHE_DIR:-$DATA_DIR/.fastembed_cache}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR" "$KB_ROOT" "$CACHE_DIR"
    chown -R pkb:pkb "$DATA_DIR"
    exec gosu pkb "$@"
fi

exec "$@"
