#!/usr/bin/env bash
#
# Compatibility shim.
#
# Superseded by scripts/start.sh, which handles both roles. Kept because a
# Railway service may still have this path as its start command — delegating
# means the deployment keeps working without anyone having to update the UI.
#
# Forces the scheduler role regardless of what MKTSCAN_ROLE says, since that is
# unambiguously what this script was for.
export MKTSCAN_ROLE=scheduler
exec "$(dirname "$0")/start.sh"
