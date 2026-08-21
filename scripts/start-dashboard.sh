#!/usr/bin/env bash
#
# Compatibility shim — superseded by scripts/start.sh. See start-scheduler.sh.
export MKTSCAN_ROLE=dashboard
exec "$(dirname "$0")/start.sh"
