#!/usr/bin/env bash
# homelab-docs-stamp — pull the homelab documentation clone and, only after a
# SUCCESSFUL fast-forward pull, write the freshness stamp that Henk's
# `homelab_docs` tool reads (read-depth task 8.3).
#
# Usage:  homelab-docs-stamp.sh <clone-dir>
# Runs as root from a daily systemd timer on rp5 against /opt/homelab-docs.
#
# Contract (openspec/changes/read-depth/notes/apply-decisions-docs.md):
#   * Stamp lives at <clone-dir>/homelab-docs-stamp.json, a JSON object with
#     "commit", "committed_at" (ISO-8601, from the commit) and "pulled_at"
#     (ISO-8601 UTC, the moment THIS successful pull finished).
#   * The stamp is written AFTER the content moved, atomically (tmp + rename in
#     the same directory), so a reader never sees a fresh stamp over stale text.
#   * A failed pull leaves the previous stamp byte-identical and exits non-zero.
#     Stamping every *attempt* would make a dead updater read as permanently
#     fresh — the exact condition the stamp exists to expose.
#   * Fast-forward only. The clone is a mirror, never an author.
#   * The stamp name is added to .git/info/exclude once, so the working tree
#     stays clean and `git clean -fd` cannot remove it.
#
# Exit codes: 0 pulled and stamped · 1 pull failed (stamp untouched) · 2 usage /
# not a clone · 75 another run holds the lock.
set -euo pipefail

STAMP_NAME="homelab-docs-stamp.json"

usage() {
    echo "usage: $(basename "$0") <clone-dir>" >&2
    exit 2
}

[[ $# -eq 1 ]] || usage
dir="$1"

if [[ ! -d "$dir" ]]; then
    echo "homelab-docs-stamp: $dir does not exist" >&2
    exit 2
fi
if ! git -C "$dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "homelab-docs-stamp: $dir is not a git working tree" >&2
    exit 2
fi
git_dir="$(git -C "$dir" rev-parse --absolute-git-dir)"

# One updater at a time; a second concurrent run leaves rather than waits.
exec 9>"$git_dir/homelab-docs-stamp.lock"
if ! flock -n 9; then
    echo "homelab-docs-stamp: another run holds the lock" >&2
    exit 75
fi

# The pull. Anything but a clean fast-forward is a failure and stops here, with
# the previous stamp untouched.
if ! git -C "$dir" pull --ff-only --quiet; then
    echo "homelab-docs-stamp: pull failed for $dir; stamp left untouched" >&2
    exit 1
fi

commit="$(git -C "$dir" rev-parse HEAD)"
committed_at="$(git -C "$dir" log -1 --format=%cI)"
pulled_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Keep the stamp invisible to git without touching the tracked .gitignore.
exclude="$git_dir/info/exclude"
mkdir -p "$(dirname "$exclude")"
touch "$exclude"
grep -qxF "$STAMP_NAME" "$exclude" || echo "$STAMP_NAME" >>"$exclude"

# Write after the content, atomically, in the same directory as the target.
tmp="$(mktemp "$dir/.${STAMP_NAME}.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
printf '{\n  "commit": "%s",\n  "committed_at": "%s",\n  "pulled_at": "%s"\n}\n' \
    "$commit" "$committed_at" "$pulled_at" >"$tmp"
chmod 0644 "$tmp"
mv -f "$tmp" "$dir/$STAMP_NAME"
trap - EXIT
echo "homelab-docs-stamp: $dir at ${commit:0:12}, pulled $pulled_at"
