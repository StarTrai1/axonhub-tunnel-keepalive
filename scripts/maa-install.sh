#!/usr/bin/env bash
# Install the reviewed upstream snapshot without replacing persistent data.
set -euo pipefail
umask 077
stack=${AXH_HOME:?AXH_HOME required}
node_bin=${AXH_NODE_BIN:-$(command -v node)}
"$node_bin" -e 'if (+process.versions.node.split(".")[0] < 22) process.exit(1)' || { echo 'MuseAutoApprove needs Node 22+ in this integration' >&2; exit 1; }
export PATH="$(dirname "$node_bin"):$PATH"
command -v npm >/dev/null || { echo 'npm missing' >&2; exit 1; }
revision=681759cf9633ea740af5e64037b667b52bb44a1b
app="$stack/MuseAutoApprove"
archive="$stack/cache/muse-guardian-$revision.tar.gz"
if [ ! -f "$app/muse-daemon.cjs" ]; then
  if [ ! -s "$archive" ]; then
    curl -fsSL --connect-timeout 10 --max-time 120 --retry 2 \
      "https://codeload.github.com/bytehola/muse-guardian/tar.gz/$revision" -o "$archive.part"
    mv "$archive.part" "$archive"
  fi
  tmp=$(mktemp -d "$stack/cache/maa.XXXXXX")
  trap 'rm -rf "$tmp"' EXIT
  tar -xzf "$archive" -C "$tmp" "muse-guardian-$revision/MuseAutoApprove"
  # A partial previous install may already hold login/session data.
  mkdir -p "$app"
  cp -a "$tmp/muse-guardian-$revision/MuseAutoApprove/work" "$app/"
  cp "$tmp/muse-guardian-$revision/MuseAutoApprove/"{muse-daemon.cjs,package.json} "$app/"
  printf '%s\n' "$revision" > "$app/.upstream-revision"
fi
mkdir -p "$app/data" "$app/log"
chmod 700 "$app" "$app/data" "$app/log"
cd "$app"
if ! "$node_bin" -e 'for (const p of ["undici", "ws", "https-proxy-agent", "sodium-native"]) require(p)' >/dev/null 2>&1; then
  # Cache and generated lockfile remain on the persistent volume for restore.
  if [ -f package-lock.json ]; then
    timeout 240 npm ci --omit=dev --prefer-offline --cache "$stack/cache/npm" --no-audit --no-fund
  else
    timeout 240 npm install --omit=dev --prefer-offline --cache "$stack/cache/npm" --no-audit --no-fund
  fi
fi
"$node_bin" --check muse-daemon.cjs
echo 'MuseAutoApprove installed; configure credentials and current MUSE_VM_ID before enabling'
