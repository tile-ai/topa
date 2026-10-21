#!/usr/bin/env bash
set -euo pipefail

# ssh finds its keys through the passwd home, /root, whatever HOME says.
install -d -m 0700 /root/.ssh
if [[ -n ${TILEOPS_HOST_SSH_DIR:-} && -d $TILEOPS_HOST_SSH_DIR ]]; then
    for path in "$TILEOPS_HOST_SSH_DIR"/*; do
        [[ -f "$path" ]] || continue
        name=${path##*/}
        [[ "$name" == environment ]] || ln -sfn "$path" "/root/.ssh/$name"
    done
fi

git_config=/root/.gitconfig
: >"$git_config"
if [[ -f /opt/tileops-host-auth/gitconfig ]]; then
    git config --file "$git_config" include.path /opt/tileops-host-auth/gitconfig
fi
# The round's directories are mounted at the paths they have on the host, so a
# command the agent writes there carries over unchanged.
if [[ -n ${TILEOPS_SAFE_DIRS:-} ]]; then
    while IFS= read -r -d: safe_dir; do
        [[ -n "$safe_dir" ]] && git config --file "$git_config" --add safe.directory "$safe_dir"
    done <<<"$TILEOPS_SAFE_DIRS:"
fi

if [[ -x /usr/local/bin/gh && -d ${GH_CONFIG_DIR:-} ]]; then
    for host in github.com gist.github.com; do
        git config --file "$git_config" --add "credential.https://$host.helper" ""
        git config --file "$git_config" --add "credential.https://$host.helper" \
            '!/usr/local/bin/gh auth git-credential'
    done
fi
chmod 0600 "$git_config"

# topa runs from the host's source, mounted read-only; it has no dependencies.
if [[ -n ${TOPA_SOURCE:-} ]]; then
    printf '#!/bin/sh\nPYTHONPATH="%s${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m topa "$@"\n' \
        "$TOPA_SOURCE" >/usr/local/bin/topa
    chmod 0755 /usr/local/bin/topa
fi

# The agent enters with docker exec; this process only keeps the container up.
exec sleep infinity
