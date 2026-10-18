#!/bin/sh
# Runs the gateway as the node user with a /data it can write.
#
# The image starts as root so that it can take over /data: a new host dataset is usually root's, and node (uid 1000)
# couldn't create the OAuth store in it. When anything in /data isn't node's, it's given to node, then the gateway runs
# as node. This needs Docker's default CHOWN, SETUID and SETGID capabilities. Started as another user (compose
# "user:"), it can't change ownership and only checks /data. Commands other than uvicorn (a shell, the tests) run as
# given.
set -e

data=${FASTMCP_HOME:-/data}

if [ "$1" = uvicorn ] && [ "$(id -u)" = 0 ]; then
    # .zfs is a ZFS dataset's snapshot directory, listed when snapdir=visible. -xdev keeps to the one filesystem.
    if [ -n "$(find "$data" -xdev -path "$data/.zfs" -prune -o ! -user node -print -quit)" ]; then
        # A fresh dataset is empty and the gateway only writes oauth-proxy/. Anything else means the wrong host path.
        other=$(find "$data" -mindepth 1 -maxdepth 1 ! -name oauth-proxy ! -name .zfs -print -quit) || true
        if [ -n "$other" ]; then
            echo "$data holds $other, which isn't the gateway's, so it won't be given to node (uid 1000)." \
                "Check the host path mounted at $data: it should be an empty dataset of its own." >&2
            exit 1
        fi
        echo "Giving $data to node (uid 1000)" >&2
        # Ownership only: a TrueNAS ACL dataset with aclmode=restricted refuses chmod, even to root.
        find "$data" -xdev -path "$data/.zfs" -prune -o ! -user node -exec chown -h node:node {} + || true
    fi
    # no-new-privs: the image's setuid binaries (su, passwd...) can't give root back.
    GATEWAY_STARTED_AS_ROOT=1 HOME=/home/node \
        exec setpriv --reuid=node --regid=node --init-groups --no-new-privs "$0" "$@"
fi

if [ "$1" = uvicorn ]; then
    # FastMCP creates and replaces files in its directories and reads them back. find also prints an error, and so
    # fails the check, for a directory it can't read or enter (|| true: set -e would exit silently on its status).
    bad=$(find "$data" -xdev -path "$data/.zfs" -prune -o \( -type d ! -writable -o -type f ! -readable \) \
        -print -quit 2>&1) || true
    if [ -n "$bad" ]; then
        if [ -n "$GATEWAY_STARTED_AS_ROOT" ]; then
            echo "uid 1000 can't use $bad, though $data was given to node where possible (see any error above)." \
                "Check that the dataset isn't read-only and that its ACL lets the owner write" \
                "(the Generic preset does)." >&2
        else
            echo "uid $(id -u) can't use $bad. Give $data to that user (chown -R $(id -u):$(id -g) on the host)," \
                "or start the container as root so that it can do this itself." >&2
        fi
        exit 1
    fi
    unset GATEWAY_STARTED_AS_ROOT
    # FastMCP's files are 600 already; this keeps its directories private too, unless an inherited ACL says otherwise.
    umask 077
fi

exec "$@"
