#!/bin/sh
# Checks that the gateway can use /data, then runs it.
#
# The image runs as node (uid 1000), and deploy/truenas.yaml runs it as apps (568:568). /data must be that user's: a
# new host dataset is root's, and FastMCP would fail to create its OAuth store in it with a PermissionError traceback.
# Commands other than uvicorn (a shell, the tests) run as given.
set -e

data=${FASTMCP_HOME:-/data}

if [ "$1" = uvicorn ]; then
    # FastMCP creates and replaces files in its directories and reads them back. find also prints an error, and so
    # fails the check, for a directory it can't read or enter (|| true: set -e would exit silently on its status).
    # .zfs is a ZFS dataset's snapshot directory, listed when snapdir=visible.
    bad=$(find "$data" -xdev -path "$data/.zfs" -prune -o \( -type d ! -writable -o -type f ! -readable \) \
        -print -quit 2>&1) || true
    if [ -n "$bad" ]; then
        echo "uid $(id -u) can't use $bad. Give $data to $(id -u):$(id -g) on the host, recursively," \
            "and check that it isn't read-only." >&2
        exit 1
    fi
    # FastMCP's files are 600 already; this keeps its directories private too, unless an inherited ACL says otherwise.
    umask 077
fi

exec "$@"
