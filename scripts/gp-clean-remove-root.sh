#!/bin/sh
# Root-only, one-way removal of a legacy GP Control Plane installation.
#
# This script deliberately does not install, fetch, stage, restore or roll back
# anything.  It removes only the fixed GP surface for one non-root install
# user.  The user-owned clean-install vault stays outside that surface and is
# checked here only as a directory boundary; root never opens its contents.
set -eu
umask 077

PATH='/usr/sbin:/usr/bin:/sbin:/bin'
export PATH

readonly CLEAN_REMOVE_ROOT='/usr/local/libexec/gp-control-plane/gp-clean-remove-root'

INSTALL_USER=''
DESTRUCTIVE_PHASE=0
PARENT_LOCKS_FILE=''
PARENT_LOCKS_HELD=0

die() {
    printf 'gp-clean-remove-root: %s\n' "$1" >&2
    return 126
}

usage() {
    printf '%s\n' \
        'usage: gp-clean-remove-root.sh --install-user USER --confirm-clean-remove' >&2
    exit 64
}

require_fixed_root_provision() {
    self_path=$(readlink -f -- "$0" 2>/dev/null || true)
    [ "$self_path" = "$CLEAN_REMOVE_ROOT" ] \
        || { die "must be provisioned and invoked from $CLEAN_REMOVE_ROOT"; return 1; }
    [ -f "$CLEAN_REMOVE_ROOT" ] && [ ! -L "$CLEAN_REMOVE_ROOT" ] \
        || { die 'provisioned clean-remove script is unsafe'; return 1; }
    [ "$(stat -c '%u:%g:%a' "$CLEAN_REMOVE_ROOT" 2>/dev/null || true)" = '0:0:700' ] \
        || { die 'provisioned clean-remove script must be root:root mode 0700'; return 1; }
}

safe_user_directory() {
    path=$1; uid=$2; label=$3
    case "$path" in /*) ;; *) die "$label is not absolute"; return 1 ;; esac
    [ -d "$path" ] && [ ! -L "$path" ] || die "$label is unsafe" || return 1
    [ "$(readlink -f -- "$path" 2>/dev/null || true)" = "$path" ] \
        || die "$label is non-canonical" || return 1
    [ "$(stat -c '%u' "$path" 2>/dev/null || true)" = "$uid" ] \
        || die "$label owner changed" || return 1
}

safe_user_private_directory() {
    safe_user_directory "$1" "$2" "$3" || return 1
    [ "$(stat -c '%a' "$1" 2>/dev/null || true)" = 700 ] \
        || die "$3 must be mode 0700" || return 1
}

validate_root_file() {
    path=$1
    [ ! -e "$path" ] && [ ! -L "$path" ] && return 0
    [ -f "$path" ] && [ ! -L "$path" ] || { die "legacy GP path is unsafe: $path"; return 1; }
    [ "$(stat -c '%u:%g' "$path" 2>/dev/null || true)" = '0:0' ] \
        || { die "legacy GP path is not root-owned: $path"; return 1; }
    mode=$(stat -c '%a' "$path" 2>/dev/null || true)
    case "$mode" in ??[2367]|?[2367]?|[2367]???) die "legacy GP path is group/world-writable: $path"; return 1 ;; esac
}

validate_root_directory() {
    path=$1
    [ ! -e "$path" ] && [ ! -L "$path" ] && return 0
    [ -d "$path" ] && [ ! -L "$path" ] || { die "legacy GP directory is unsafe: $path"; return 1; }
    [ "$(readlink -f -- "$path" 2>/dev/null || true)" = "$path" ] \
        || { die "legacy GP directory is non-canonical: $path"; return 1; }
    [ "$(stat -c '%u:%g' "$path" 2>/dev/null || true)" = '0:0' ] \
        || { die "legacy GP directory is not root-owned: $path"; return 1; }
    mode=$(stat -c '%a' "$path" 2>/dev/null || true)
    case "$mode" in ??[2367]|?[2367]?|[2367]???) die "legacy GP directory is group/world-writable: $path"; return 1 ;; esac
}

validate_unit_path() {
    path=$1
    [ ! -e "$path" ] && [ ! -L "$path" ] && return 0
    if [ -L "$path" ]; then
        [ "$(readlink -- "$path" 2>/dev/null || true)" = /dev/null ] \
            || { die "GP unit path is an unsafe link: $path"; return 1; }
    else
        [ -f "$path" ] || { die "GP unit path is not a regular file: $path"; return 1; }
        [ "$(stat -c '%u' "$path" 2>/dev/null || true)" = 0 ] \
            || { die "GP unit path is not root-owned: $path"; return 1; }
    fi
}

validate_root_helper_directory() {
    helper_dir=/usr/local/libexec/gp-control-plane
    [ ! -e "$helper_dir" ] && [ ! -L "$helper_dir" ] && return 0
    validate_root_directory "$helper_dir" || return 1
    for member in "$helper_dir"/* "$helper_dir"/.[!.]* "$helper_dir"/..?*; do
        [ -e "$member" ] || [ -L "$member" ] || continue
        case "$member" in
            "$helper_dir/gp-root-helper"|"$helper_dir/gp-clean-remove-root") ;;
            *)
                die "legacy root-helper directory contains a foreign path: $member"
                return 1
            ;;
        esac
        validate_root_file "$member" || return 1
    done
}

validate_removal_surface() {
    validate_unit_path /etc/systemd/system/gp-control-plane-core.service || return 1
    validate_unit_path /etc/systemd/system/gp-control-plane-web.service || return 1
    validate_unit_path /run/systemd/system/gp-control-plane-core.service || return 1
    validate_unit_path /run/systemd/system/gp-control-plane-web.service || return 1
    validate_root_file /etc/default/gp-control-plane-install-profile || return 1
    validate_root_file /etc/default/gp-control-plane-core || return 1
    validate_root_file /etc/default/gp-control-plane-web || return 1
    validate_root_file /etc/default/gp-control-plane-root-helper || return 1
    validate_root_file /etc/sudoers.d/gp-control-plane-root-helper || return 1
    validate_root_helper_directory || return 1
    validate_root_directory /run/gp-control-plane/runs || return 1
    validate_root_directory /run/gp-control-plane/gates || return 1
    validate_root_directory /var/lib/gp-control-plane/release-gates || return 1
}

validate_fixed_paths() {
    uid=$(id -u "$INSTALL_USER") || return 1
    [ "$INSTALL_USER" != root ] || { die 'root is not a supported install user'; return 1; }
    TARGET_HOME=$(getent passwd "$INSTALL_USER" | cut -d: -f6) || return 1
    [ -n "$TARGET_HOME" ] || { die 'cannot resolve install-user home'; return 1; }
    [ "$TARGET_HOME" = "$(readlink -f -- "$TARGET_HOME" 2>/dev/null || true)" ] \
        || { die 'install-user home is non-canonical'; return 1; }
    GP_ROOT="$TARGET_HOME/gp"
    INSTALL_DIR="$GP_ROOT/GP-access-control-plane"
    CURRENT_STATE_ROOT="$GP_ROOT/.GP-access-control-plane.data"
    CURRENT_STATE_DIR="$CURRENT_STATE_ROOT/state"
    LEGACY_STATE_DIR="$INSTALL_DIR/build/state"
    VAULT_DIR="$TARGET_HOME/.local/share/gp-control-plane/clean-install-vault"

    safe_user_directory "$TARGET_HOME" "$uid" 'install-user home' || return 1
    safe_user_directory "$GP_ROOT" "$uid" 'managed GP parent' || return 1
    safe_user_directory "$INSTALL_DIR" "$uid" 'managed install directory' || return 1

    legacy_state_present=0
    current_state_present=0
    if [ -e "$LEGACY_STATE_DIR" ] || [ -L "$LEGACY_STATE_DIR" ]; then
        safe_user_directory "$INSTALL_DIR/build" "$uid" 'legacy state parent' || return 1
        safe_user_directory "$LEGACY_STATE_DIR" "$uid" 'legacy state directory' || return 1
        legacy_state_present=1
    fi
    if [ -e "$CURRENT_STATE_ROOT" ] || [ -L "$CURRENT_STATE_ROOT" ]; then
        safe_user_directory "$CURRENT_STATE_ROOT" "$uid" 'current state root' || return 1
        safe_user_directory "$CURRENT_STATE_DIR" "$uid" 'current state directory' || return 1
        current_state_present=1
    fi
    [ "$legacy_state_present" -eq 1 ] || [ "$current_state_present" -eq 1 ] \
        || { die 'neither supported legacy nor current GP state layout exists'; return 1; }

    for vault_parent in "$TARGET_HOME/.local" "$TARGET_HOME/.local/share" "$TARGET_HOME/.local/share/gp-control-plane"; do
        safe_user_directory "$vault_parent" "$uid" 'clean-install vault parent' || return 1
    done
    safe_user_private_directory "$VAULT_DIR" "$uid" 'clean-install vault' || return 1
}

record_and_lock_parent() {
    path=$1; uid=$2
    if awk -F'|' -v wanted="$path" '$1 == wanted { found=1 } END { exit found ? 0 : 1 }' "$PARENT_LOCKS_FILE" 2>/dev/null; then
        return 0
    fi
    safe_user_directory "$path" "$uid" 'managed parent' || return 1
    saved_gid=$(stat -c '%g' "$path") || return 1
    saved_mode=$(stat -c '%a' "$path") || return 1
    printf '%s|%s|%s|%s\n' "$path" "$uid" "$saved_gid" "$saved_mode" >> "$PARENT_LOCKS_FILE" || return 1
    chown root:root "$path" || return 1
    chmod 0711 "$path" || return 1
    [ "$(stat -c '%u:%g:%a' "$path" 2>/dev/null || true)" = '0:0:711' ] || return 1
}

acquire_parent_locks() {
    uid=$(id -u "$INSTALL_USER") || return 1
    PARENT_LOCKS_FILE=$(mktemp /run/gp-clean-remove.parents.XXXXXX) || return 1
    chmod 0600 "$PARENT_LOCKS_FILE" || return 1
    PARENT_LOCKS_HELD=1
    record_and_lock_parent "$TARGET_HOME" "$uid" || return 1
    record_and_lock_parent "$GP_ROOT" "$uid" || return 1
}

revalidate_locked_fixed_paths() {
    uid=$(id -u "$INSTALL_USER") || return 1
    [ "$(stat -c '%u:%g:%a' "$TARGET_HOME" 2>/dev/null || true)" = '0:0:711' ] \
        || { die 'install-user home lock changed'; return 1; }
    [ "$(stat -c '%u:%g:%a' "$GP_ROOT" 2>/dev/null || true)" = '0:0:711' ] \
        || { die 'managed GP parent lock changed'; return 1; }
    [ "$(readlink -f -- "$TARGET_HOME" 2>/dev/null || true)" = "$TARGET_HOME" ] \
        || { die 'install-user home lock is non-canonical'; return 1; }
    [ "$(readlink -f -- "$GP_ROOT" 2>/dev/null || true)" = "$GP_ROOT" ] \
        || { die 'managed GP parent lock is non-canonical'; return 1; }

    safe_user_directory "$INSTALL_DIR" "$uid" 'managed install directory after lock' || return 1
    if [ "$legacy_state_present" -eq 1 ]; then
        safe_user_directory "$INSTALL_DIR/build" "$uid" 'legacy state parent after lock' || return 1
        safe_user_directory "$LEGACY_STATE_DIR" "$uid" 'legacy state directory after lock' || return 1
    else
        [ ! -e "$LEGACY_STATE_DIR" ] && [ ! -L "$LEGACY_STATE_DIR" ] \
            || { die 'unexpected legacy state layout appeared after lock'; return 1; }
    fi
    if [ "$current_state_present" -eq 1 ]; then
        safe_user_directory "$CURRENT_STATE_ROOT" "$uid" 'current state root after lock' || return 1
        safe_user_directory "$CURRENT_STATE_DIR" "$uid" 'current state directory after lock' || return 1
    else
        [ ! -e "$CURRENT_STATE_ROOT" ] && [ ! -L "$CURRENT_STATE_ROOT" ] \
            || { die 'unexpected current state layout appeared after lock'; return 1; }
    fi
    for vault_parent in "$TARGET_HOME/.local" "$TARGET_HOME/.local/share" "$TARGET_HOME/.local/share/gp-control-plane"; do
        safe_user_directory "$vault_parent" "$uid" 'clean-install vault parent after lock' || return 1
    done
    safe_user_private_directory "$VAULT_DIR" "$uid" 'clean-install vault after lock' || return 1
}

release_parent_locks() {
    [ "$PARENT_LOCKS_HELD" = 1 ] || return 0
    while IFS='|' read -r path saved_uid saved_gid saved_mode; do
        [ -n "$path" ] || continue
        [ -d "$path" ] && [ ! -L "$path" ] || return 1
        chown "$saved_uid:$saved_gid" "$path" || return 1
        chmod "$saved_mode" "$path" || return 1
    done < "$PARENT_LOCKS_FILE"
    rm -f -- "$PARENT_LOCKS_FILE"
    PARENT_LOCKS_HELD=0
}

quiesce_gp_service() {
    unit=$1
    load_state=$(systemctl show --property=LoadState --value "$unit" 2>/dev/null) \
        || { die "cannot query $unit"; return 1; }
    case "$load_state" in not-found) return 0 ;; loaded) ;; *) die "unexpected LoadState for $unit: $load_state"; return 1 ;; esac
    if systemctl is-active --quiet "$unit"; then
        systemctl stop "$unit" || return 1
        systemctl is-active --quiet "$unit" && { die "$unit stayed active"; return 1; }
    fi
    systemctl unmask "$unit" >/dev/null 2>&1 || true
    systemctl disable "$unit" >/dev/null 2>&1 || return 1
}

remove_unit_path() {
    path=$1
    [ ! -e "$path" ] && [ ! -L "$path" ] || rm -f -- "$path" || return 1
}

remove_old_gp_surface() {
    DESTRUCTIVE_PHASE=1
    quiesce_gp_service gp-control-plane-web.service || return 1
    quiesce_gp_service gp-control-plane-core.service || return 1
    remove_unit_path /etc/systemd/system/gp-control-plane-core.service || return 1
    remove_unit_path /etc/systemd/system/gp-control-plane-web.service || return 1
    remove_unit_path /run/systemd/system/gp-control-plane-core.service || return 1
    remove_unit_path /run/systemd/system/gp-control-plane-web.service || return 1
    rm -f -- /etc/default/gp-control-plane-install-profile || return 1
    rm -f -- /etc/default/gp-control-plane-core || return 1
    rm -f -- /etc/default/gp-control-plane-web || return 1
    rm -f -- /etc/default/gp-control-plane-root-helper || return 1
    rm -f -- /etc/sudoers.d/gp-control-plane-root-helper || return 1
    rm -f -- /usr/local/libexec/gp-control-plane/gp-root-helper || return 1
    rm -f -- /usr/local/libexec/gp-control-plane/gp-clean-remove-root || return 1
    rmdir -- /usr/local/libexec/gp-control-plane 2>/dev/null || {
        [ ! -e /usr/local/libexec/gp-control-plane ] && [ ! -L /usr/local/libexec/gp-control-plane ] || return 1
    }
    rm -rf --one-file-system -- /run/gp-control-plane/runs || return 1
    rm -rf --one-file-system -- /run/gp-control-plane/gates || return 1
    rm -rf --one-file-system -- /var/lib/gp-control-plane/release-gates || return 1
    systemctl daemon-reload || return 1
    rm -rf --one-file-system -- "$INSTALL_DIR" || return 1
    if [ -e "$CURRENT_STATE_ROOT" ] || [ -L "$CURRENT_STATE_ROOT" ]; then
        rm -rf --one-file-system -- "$CURRENT_STATE_ROOT" || return 1
    fi
}

cleanup() {
    release_parent_locks || true
}

on_signal() {
    signal=$1
    if [ "$DESTRUCTIVE_PHASE" = 0 ]; then
        printf 'status=failed phase=pre-clean signal=%s\n' "$signal" >&2
    else
        printf 'status=failed phase=clean-remove signal=%s\n' "$signal" >&2
    fi
    exit 126
}

[ "$(id -u)" -eq 0 ] || { die 'must be executed as root'; exit 126; }
[ "$#" -eq 3 ] || usage
[ "$1" = --install-user ] && [ "$3" = --confirm-clean-remove ] || usage
INSTALL_USER=$2
case "$INSTALL_USER" in ''|root|*[!A-Za-z0-9_-]*) die 'install user is invalid'; exit 64 ;; esac
require_fixed_root_provision || exit 126
trap 'on_signal HUP' HUP
trap 'on_signal INT' INT
trap 'on_signal TERM' TERM
trap cleanup EXIT

if validate_fixed_paths && validate_removal_surface && acquire_parent_locks && revalidate_locked_fixed_paths && remove_old_gp_surface; then
    printf '%s\n' 'status=success phase=clean-remove'
    exit 0
fi
if [ "$DESTRUCTIVE_PHASE" = 0 ]; then
    printf '%s\n' 'status=failed phase=pre-clean' >&2
else
    printf '%s\n' 'status=failed phase=clean-remove' >&2
fi
exit 126
