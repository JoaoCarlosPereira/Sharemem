#!/bin/bash
# Mem0 Shared entrypoint wrapper for PLANKA.
# Creates schema `planka`, injects search_path into DATABASE_URL, then runs upstream start.sh.
set -eu

export MEM0_PG_SCHEMA="${MEM0_PG_SCHEMA:-planka}"

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "start-mem0: DATABASE_URL is required" >&2
  exit 1
fi

# Ensure schema exists (idempotent; safe on existing Postgres volumes).
# Note: tables/next_id() are created later by Knex in start.sh; that script
# re-runs ensure-schema after migrate to qualify next_id_seq + public views.
node ./mem0/ensure-schema.js

# Inject search_path for node-pg / sails-postgresql when not already present.
if [[ "${DATABASE_URL}" != *"search_path"* ]]; then
  if [[ "${DATABASE_URL}" == *"?"* ]]; then
    export DATABASE_URL="${DATABASE_URL}&options=-csearch_path%3D${MEM0_PG_SCHEMA}%2Cpublic"
  else
    export DATABASE_URL="${DATABASE_URL}?options=-csearch_path%3D${MEM0_PG_SCHEMA}%2Cpublic"
  fi
fi

exec ./start.sh
