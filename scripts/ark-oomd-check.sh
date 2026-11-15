#!/bin/sh
# Checks that systemd-oomd will not pick the ARK game container when memory runs short.
# compose.asa.yaml marks the server container ManagedOOMPreference=omit; systemd-oomd
# honours that only for pressure kills of a cgroup owned by the same user (user@UID.service).
# Read-only: no sudo, nothing is changed. Exit 0 = protected, 1 = not protected, 2 = unknown.
set -u
SOCKET=${ARK_DOCKER_SOCKET:-/run/user/$(id -u)/ark-docker.sock}
CONTAINER=${1:-ark-asa-server-1}

pid=$(docker --host "unix://$SOCKET" inspect -f '{{.State.Pid}}' "$CONTAINER" 2>/dev/null) || {
    echo "unknown: container $CONTAINER is not running (start it, then run this again)"; exit 2; }
cgroup=/sys/fs/cgroup$(cut -d: -f3 "/proc/$pid/cgroup")
monitored=/sys/fs/cgroup/user.slice/user-$(id -u).slice/user@$(id -u).service

marked=$(python3 -c 'import os,sys; print("user.oomd_omit" in os.listxattr(sys.argv[1]))' "$cgroup" 2>/dev/null)
case "$cgroup" in "$monitored"/*) inside=yes ;; *) inside=no ;; esac
owner_same=no
[ "$(stat -c %u "$cgroup")" = "$(stat -c %u "$monitored")" ] && owner_same=yes

echo "game cgroup:        $cgroup"
echo "omit mark:          $marked"
echo "under user manager: $inside"
echo "same owner:         $owner_same"
if [ "$marked" = True ] && [ "$inside" = yes ] && [ "$owner_same" = yes ]; then
    echo "protected: memory-pressure kills will choose other apps first"
    exit 0
fi
echo "NOT protected: recreate the server with compose.asa.yaml (COMPOSE_FILE in .env) and check again"
exit 1
