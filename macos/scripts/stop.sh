#!/bin/sh
# Thin wrapper: bootout the LaunchDaemon and restore the saved system proxy.
set -eu
. "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/_common.sh"
require_root stop
run_cli stop "$@"
