#!/usr/bin/env bash
# This script is the sole elevated process of a clean installation.
set -Eeuo pipefail
PATH='/usr/sbin:/usr/bin:/sbin:/bin'
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
usage() { printf '%s\n' 'usage: install-linux.sh --source-dir DIR --install-user USER (--tag vX.Y.Z | --candidate-sha SHA) --web on|off --initial-install on|off' >&2; exit 64; }
[ "$(id -u)" -eq 0 ] || fail 'must be run by the bootstrap sudo process'
SOURCE_DIR= INSTALL_USER= TAG= CANDIDATE_SHA= INSTALL_WEB= INITIAL_INSTALL=
while [ "$#" -gt 0 ]; do
  case "$1" in
    --source-dir|--install-user|--tag|--candidate-sha|--web|--initial-install)
      [ "$#" -ge 2 ] || usage; value="$2"; case "$1" in --source-dir) SOURCE_DIR="$value";; --install-user) INSTALL_USER="$value";; --tag) TAG="$value";; --candidate-sha) CANDIDATE_SHA="$value";; --web) INSTALL_WEB="$value";; --initial-install) INITIAL_INSTALL="$value";; esac; shift 2 ;;
    *) usage ;;
  esac
done
case "$INSTALL_USER" in ''|root|*[!A-Za-z0-9_-]*) fail 'install user is invalid';; esac
case "$INSTALL_WEB" in on|off) ;; *) fail 'web mode must be on or off';; esac
case "$INITIAL_INSTALL" in on|off) ;; *) fail 'initial-install must be on or off';; esac
[ -d "$SOURCE_DIR/.git" ] && [ ! -L "$SOURCE_DIR" ] || fail 'source directory is unsafe'
[ -z "$TAG" ] || [ -z "$CANDIDATE_SHA" ] || usage
[ -n "$TAG" ] || [ -n "$CANDIDATE_SHA" ] || usage
if [ -n "$TAG" ]; then
  printf '%s\n' "$TAG" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$' || fail 'tag must be an exact release tag'
  [ "$(git -C "$SOURCE_DIR" cat-file -t "refs/tags/$TAG" 2>/dev/null || true)" = tag ] || fail 'source tag must be annotated'
  [ "$(git -C "$SOURCE_DIR" rev-parse HEAD)" = "$(git -C "$SOURCE_DIR" rev-parse "refs/tags/$TAG^{commit}")" ] || fail 'source checkout does not match the exact tag'
else
  printf '%s\n' "$CANDIDATE_SHA" | grep -Eq '^[0-9a-f]{40}$' || fail 'candidate SHA must be a full lowercase commit SHA'
  [ "$(git -C "$SOURCE_DIR" rev-parse HEAD)" = "$CANDIDATE_SHA" ] || fail 'source checkout does not match the exact candidate SHA'
fi
[ -z "$(git -C "$SOURCE_DIR" status --porcelain)" ] || fail 'exact-tag source tree is not clean'
target_home="$(getent passwd "$INSTALL_USER" | cut -d: -f6)"
[ -n "$target_home" ] && [ -d "$target_home" ] || fail 'install-user home is unavailable'
gp_root="$target_home/gp"; install_dir="$gp_root/GP-access-control-plane"; legacy_state="$install_dir/build/state"; state_parent="$gp_root/.GP-access-control-plane.data"; state_dir="$state_parent/state"
[ "$target_home" = "$(readlink -f -- "$target_home")" ] && [ ! -L "$target_home" ] || fail 'install-user home is not canonical'
if [ -e "$gp_root" ] || [ -L "$gp_root" ]; then
  [ -d "$gp_root" ] && [ ! -L "$gp_root" ] && [ "$gp_root" = "$(readlink -f -- "$gp_root")" ] || fail 'managed GP root is not canonical'
elif [ "$INITIAL_INSTALL" != on ]; then
  fail 'managed GP root is unavailable for a vault restore'
fi
if [ -e "$install_dir" ] || [ -L "$install_dir" ]; then
  [ -d "$install_dir" ] && [ ! -L "$install_dir" ] && [ "$install_dir" = "$(readlink -f -- "$install_dir")" ] || fail 'managed GP install directory is not canonical'
fi
vault_tool="$SOURCE_DIR/scripts/clean-install-vault.py"
[ -f "$vault_tool" ] && [ ! -L "$vault_tool" ] || fail 'exact tag lacks the vault tool'
# This executes as the install user; root neither reads nor deletes the vault/handoff.
if [ "$INITIAL_INSTALL" = off ]; then
  runuser -u "$INSTALL_USER" -- python3 "$vault_tool" --verify --state-dir "$legacy_state" --home "$target_home" >/dev/null || fail 'vault is absent or corrupt; nothing was removed'
fi
stop_unit() { systemctl disable --now "$1" >/dev/null 2>&1 || true; }
stop_unit gp-control-plane-web.service; stop_unit gp-control-plane-core.service
rm -f -- /etc/systemd/system/gp-control-plane-core.service /etc/systemd/system/gp-control-plane-web.service
rm -f -- /etc/default/gp-control-plane-install-profile /etc/default/gp-control-plane-core /etc/default/gp-control-plane-web /etc/default/gp-control-plane-root-helper /etc/sudoers.d/gp-control-plane-root-helper
rm -rf --one-file-system -- /usr/local/libexec/gp-control-plane /run/gp-control-plane "$install_dir" "$state_parent"
systemctl daemon-reload
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl dnsutils git iproute2 ipset iptables nftables python3 python3-pip python3-venv sudo
group="$(id -gn "$INSTALL_USER")"
install -d -o "$INSTALL_USER" -g "$group" "$gp_root" "$install_dir"
tar -C "$SOURCE_DIR" --exclude=.git -cf - . | tar -C "$install_dir" -xf -
chown -R "$INSTALL_USER:$group" "$install_dir"
ZAPRET_DIR=/opt/zapret2 bash "$install_dir/scripts/install-zapret2.sh"
[ -x /opt/zapret2/blockcheck2.sh ] && [ -x /opt/zapret2/nfq2/nfqws2 ] || fail 'zapret2 runtime is not ready'
install -d -m 0755 /usr/local/libexec/gp-control-plane
cat > /usr/local/libexec/gp-control-plane/nfqws2 <<'EOF'
#!/bin/sh
exec /opt/zapret2/nfq2/nfqws2 "$@"
EOF
cat > /usr/local/libexec/gp-control-plane/blockcheck2.sh <<'EOF'
#!/bin/sh
exec /opt/zapret2/blockcheck2.sh "$@"
EOF
chmod 0755 /usr/local/libexec/gp-control-plane/nfqws2 /usr/local/libexec/gp-control-plane/blockcheck2.sh
install -d -m 0700 -o "$INSTALL_USER" -g "$group" "$state_parent" "$state_dir"
runuser -u "$INSTALL_USER" -- python3 -m venv "$install_dir/.venv"
runuser -u "$INSTALL_USER" -- "$install_dir/.venv/bin/python" -m pip install --upgrade pip setuptools wheel
runuser -u "$INSTALL_USER" -- "$install_dir/.venv/bin/python" -m pip install -e "$install_dir"
# The v2fly cache is disposable service data.  A network failure must not undo a
# successful clean install: the authenticated Web action can retry it later.
if ! runuser -u "$INSTALL_USER" -- env GP_STATE_DIR="$state_dir" "$install_dir/.venv/bin/gp-control-plane" domain-sources prepare-v2fly; then
  printf '%s\n' 'WARNING: v2fly catalog was not prepared; start the service and retry from the Web interface.' >&2
fi
install -d -m 0755 /usr/local/libexec/gp-control-plane
install -m 0755 "$install_dir/scripts/gp-root-helper.sh" /usr/local/libexec/gp-control-plane/gp-root-helper
cat > /etc/sudoers.d/gp-control-plane-root-helper <<EOF
# Managed by GP clean installer; runtime discovery only.
$INSTALL_USER ALL=(root) NOPASSWD: /usr/local/libexec/gp-control-plane/gp-root-helper *
EOF
visudo -cf /etc/sudoers.d/gp-control-plane-root-helper >/dev/null || fail 'runtime sudoers rule is invalid'
chmod 0440 /etc/sudoers.d/gp-control-plane-root-helper
cat > /etc/default/gp-control-plane-core <<EOF
GP_INSTALL_DIR='$install_dir'
GP_STATE_DIR='$state_dir'
EOF
cat > /etc/systemd/system/gp-control-plane-core.service <<EOF
[Unit]
Description=GP Strategy Finder Core API
After=network-online.target
[Service]
User=$INSTALL_USER
WorkingDirectory=$install_dir
Environment=HOME=$target_home
Environment=PATH=/usr/local/libexec/gp-control-plane:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
EnvironmentFile=/etc/default/gp-control-plane-core
Environment=GP_ROOT_HELPER=/usr/local/libexec/gp-control-plane/gp-root-helper
ExecStart=$install_dir/.venv/bin/gp-control-plane core --host 127.0.0.1 --port 8081
Restart=always
[Install]
WantedBy=multi-user.target
EOF
if [ "$INSTALL_WEB" = on ]; then
cat > /etc/default/gp-control-plane-web <<EOF
GP_INSTALL_DIR='$install_dir'
GP_STATE_DIR='$state_dir'
EOF
cat > /etc/systemd/system/gp-control-plane-web.service <<EOF
[Unit]
Description=GP Strategy Finder Web UI
After=network-online.target gp-control-plane-core.service
[Service]
User=$INSTALL_USER
WorkingDirectory=$install_dir
Environment=HOME=$target_home
Environment=PATH=/usr/local/libexec/gp-control-plane:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
EnvironmentFile=/etc/default/gp-control-plane-web
ExecStart=$install_dir/.venv/bin/gp-control-plane web --host 0.0.0.0 --port 8080 --core-url http://127.0.0.1:8081
Restart=always
[Install]
WantedBy=multi-user.target
EOF
fi
systemctl daemon-reload; systemctl enable --now gp-control-plane-core.service
if [ "$INSTALL_WEB" = on ]; then systemctl enable --now gp-control-plane-web.service; fi
printf '%s\n' 'status=success phase=fresh-install'
