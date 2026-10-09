#!/usr/bin/env bash
# Offline test for sync-from-build.sh: builds a fixture page in a temp dir and checks
# what lands in the deploy directory. Run: site/page/test-sync.sh
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
SYNC="$here/sync-from-build.sh"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
# stand-in for the repo's untracked .githooks/local-checks.sh: same contract
# (path to a file of text as $1, non-zero exit blocks), fails on a test marker
cat > "$TMP/checks.sh" <<'EOF2'
#!/usr/bin/env bash
! grep -q PRIVATE-MARKER "$1"
EOF2
chmod +x "$TMP/checks.sh"
export LOCAL_CHECKS="$TMP/checks.sh"
FAILED=0
ok()  { printf '  PASS %s\n' "$1"; }
bad() { printf '  FAIL %s\n' "$1"; FAILED=1; }

fixture() {  # $1 = dir
  mkdir -p "$1/frames"
  printf 'x' > "$1/frames/R1-0.webp"; printf 'x' > "$1/ornament.webp"; printf 'x' > "$1/og-image.png"
  for f in favicon.ico favicon-32.png apple-touch-icon.png; do printf 'x' > "$1/$f"; done
  cat > "$1/index.html" <<'EOF'
<meta property="og:image" content="https://henk.hulsman.dev/og-image.png">
<link rel="icon" href="favicon-32.png" sizes="32x32" type="image/png">
<a class="pill" href="/blog/henk-watchdog">The watchdog write-up</a>
<a href="https://hulsman.dev">hulsman.dev</a>
EOF
  cat > "$1/henk-public.json" <<'EOF'
{"generated_at":"x","state":"ok","incident":null,"today":{"alerts_seen":212},
 "since_launch":{"days":70},"last_message":{"sent_at":"2026-09-26T17:59:00+02:00","text":"a\nb"},
 "incident_example":{"state":"incident"}}
EOF
}

echo "=== a clean build syncs into the deploy dir ==="
fixture "$TMP/build"; mkdir -p "$TMP/public"; printf 'stale' > "$TMP/public/old.webp"
if "$SYNC" "$TMP/build" "$TMP/public" >/dev/null 2>&1; then ok "exit 0"; else bad "sync failed"; fi
grep -q 'href="https://hulsman.dev/blog/henk-watchdog"' "$TMP/public/index.html" \
  && ok "blog link made absolute" || bad "blog link not rewritten"
grep -q 'href="/' "$TMP/public/index.html" && bad "a root-relative link survived" || ok "no root-relative links"
keys=$(jq -c 'keys' "$TMP/public/henk-public.json")
[ "$keys" = '["incident","last_message","state"]' ] && ok "feed carries only state, incident, last_message" \
  || bad "feed keys: $keys"
[ -f "$TMP/public/frames/R1-0.webp" ] && [ -f "$TMP/public/ornament.webp" ] && [ -f "$TMP/public/og-image.png" ] \
  && ok "frames, ornament and preview image copied" || bad "assets missing"
[ -f "$TMP/public/favicon.ico" ] && [ -f "$TMP/public/favicon-32.png" ] && [ -f "$TMP/public/apple-touch-icon.png" ] \
  && ok "favicons copied" || bad "assets missing"
[ -e "$TMP/public/old.webp" ] && bad "stale file left in deploy dir" || ok "stale files removed"

echo "=== a build the local publication check rejects is refused, deploy dir untouched ==="
fixture "$TMP/b2"; echo '<p>PRIVATE-MARKER</p>' >> "$TMP/b2/index.html"
cp -r "$TMP/public" "$TMP/before"
if "$SYNC" "$TMP/b2" "$TMP/public" >/dev/null 2>&1; then bad "private string accepted"; else ok "refused"; fi
diff -r "$TMP/before" "$TMP/public" >/dev/null && ok "deploy dir unchanged" || bad "deploy dir changed on failure"

echo "=== a build missing a file is refused ==="
fixture "$TMP/b3"; rm "$TMP/b3/ornament.webp"
if "$SYNC" "$TMP/b3" "$TMP/public" >/dev/null 2>&1; then bad "incomplete build accepted"; else ok "refused"; fi

echo "=== a build without a preview image is refused ==="
fixture "$TMP/b6"; rm "$TMP/b6/og-image.png"
if "$SYNC" "$TMP/b6" "$TMP/public" >/dev/null 2>&1; then bad "build without og-image.png accepted"; else ok "refused"; fi

echo "=== a build without favicons, or a page that does not link one, is refused ==="
fixture "$TMP/b9"; rm "$TMP/b9/favicon-32.png"
if "$SYNC" "$TMP/b9" "$TMP/public" >/dev/null 2>&1; then bad "build without favicon accepted"; else ok "refused (file)"; fi
fixture "$TMP/b10"; sed -i '/rel="icon"/d' "$TMP/b10/index.html"
if "$SYNC" "$TMP/b10" "$TMP/public" >/dev/null 2>&1; then bad "page without icon link accepted"; else ok "refused (link)"; fi

echo "=== a page whose preview tag is missing or relative is refused ==="
fixture "$TMP/b7"; sed -i '/og:image/d' "$TMP/b7/index.html"
if "$SYNC" "$TMP/b7" "$TMP/public" >/dev/null 2>&1; then bad "page without og:image accepted"; else ok "refused (missing)"; fi
fixture "$TMP/b8"; sed -i 's#content="https://henk.hulsman.dev/og-image.png"#content="/og-image.png"#' "$TMP/b8/index.html"
if "$SYNC" "$TMP/b8" "$TMP/public" >/dev/null 2>&1; then bad "relative og:image accepted"; else ok "refused (relative)"; fi

echo "=== a feed without a message is refused ==="
fixture "$TMP/b4"; echo '{"state":"ok"}' > "$TMP/b4/henk-public.json"
if "$SYNC" "$TMP/b4" "$TMP/public" >/dev/null 2>&1; then bad "empty feed accepted"; else ok "refused"; fi

echo "=== without the local publication check, sync refuses (fails closed) ==="
fixture "$TMP/b5"
if LOCAL_CHECKS="$TMP/no-such-checks.sh" "$SYNC" "$TMP/b5" "$TMP/public" >/dev/null 2>&1; then
  bad "synced without the publication check"; else ok "refused"; fi

echo
[ "$FAILED" -eq 0 ] && echo "ALL SYNC TESTS PASSED" || { echo "FAILURES ABOVE"; exit 1; }
