#!/usr/bin/env bash
#
# Copy a finished page build into public/, ready for `wrangler deploy`.
#
#   Usage: site/page/sync-from-build.sh [BUILD_DIR] [PUBLIC_DIR]
#          BUILD_DIR   default site/builds/b-tile-v3 (untracked)
#          PUBLIC_DIR  default site/page/public
#          LOCAL_CHECKS  default .githooks/local-checks.sh (untracked, see AGENTS.md)
#
# What it changes on the way:
#   - root-relative links (href="/blog/...") become https://hulsman.dev/..., because the
#     page is served from henk.hulsman.dev, where those paths do not exist
#   - henk-public.json keeps only state, incident and last_message: the mock's
#     illustrative counts are never published
# og-image.png is the 1200x630 link preview; index.html must point og:image at its absolute
# https://henk.hulsman.dev URL, because link scrapers ignore relative image paths.
# It refuses, leaving PUBLIC_DIR untouched, when a file is missing, the feed has no
# message, or the repo's local publication check rejects the page text. That check is
# untracked on purpose, so sync fails closed when it is absent.
#
# Test: site/page/test-sync.sh
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
src=${1:-$here/../builds/b-tile-v3}
dest=${2:-$here/public}
checks=${LOCAL_CHECKS:-$here/../../.githooks/local-checks.sh}

die() { echo "sync: $*" >&2; exit 1; }

command -v jq >/dev/null || die "jq required"
for f in index.html ornament.webp og-image.png henk-public.json; do
  [ -f "$src/$f" ] || die "missing $src/$f"
done
[ -d "$src/frames" ] || die "missing $src/frames/"
[ -x "$checks" ] || die "local publication check not found at $checks (copy it from the private config repo)"

stage=$(mktemp -d); trap 'rm -rf "$stage"' EXIT
mkdir "$stage/public"
cp -r "$src/frames" "$src/ornament.webp" "$src/og-image.png" "$stage/public/"
sed 's#href="/\([^/"]\)#href="https://hulsman.dev/\1#g' "$src/index.html" > "$stage/public/index.html"
jq '{state, incident, last_message}' "$src/henk-public.json" > "$stage/public/henk-public.json"

jq -e '.last_message.text | type == "string" and length > 0' "$stage/public/henk-public.json" >/dev/null \
  || die "henk-public.json has no last_message.text"
if grep -q 'href="/[^/]' "$stage/public/index.html"; then die "root-relative link left in index.html"; fi
grep -q '<meta property="og:image" content="https://henk.hulsman.dev/og-image.png">' "$stage/public/index.html" \
  || die "index.html needs og:image pointing at https://henk.hulsman.dev/og-image.png"
cat "$stage/public/index.html" "$stage/public/henk-public.json" > "$stage/text"
"$checks" "$stage/text" || die "local publication check rejected the page"

rm -rf "$dest"
mv "$stage/public" "$dest"
echo "synced $src -> $dest"
