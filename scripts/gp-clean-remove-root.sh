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
readonly CLEAN_REMOVE_PREFLIGHT='/usr/local/libexec/gp-control-plane/gp-clean-remove-preflight'
readonly CLEAN_REMOVE_MANIFEST='/usr/local/libexec/gp-control-plane/gp-clean-remove-root.manifest'

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

is_sha40() {
    case "${1:-}" in ????????????????????????????????????????) ;; *) return 1 ;; esac
    case "$1" in *[!0-9a-f]*) return 1 ;; esac
}

is_sha256() {
    case "${1:-}" in ????????????????????????????????????????????????????????????????) ;; *) return 1 ;; esac
    case "$1" in *[!0-9a-f]*) return 1 ;; esac
}

read_strict_manifest() {
    manifest_candidate_sha=$(awk -F= '$1 == "candidate_sha" { count++; value=$2 } END { if (count != 1) exit 1; print value }' "$CLEAN_REMOVE_MANIFEST" 2>/dev/null || true)
    manifest_cleaner_sha256=$(awk -F= '$1 == "cleaner_sha256" { count++; value=$2 } END { if (count != 1) exit 1; print value }' "$CLEAN_REMOVE_MANIFEST" 2>/dev/null || true)
    manifest_preflight_sha256=$(awk -F= '$1 == "preflight_sha256" { count++; value=$2 } END { if (count != 1) exit 1; print value }' "$CLEAN_REMOVE_MANIFEST" 2>/dev/null || true)
    manifest_cleaner_path=$(awk -F= '$1 == "cleaner_path" { count++; value=$2 } END { if (count != 1) exit 1; print value }' "$CLEAN_REMOVE_MANIFEST" 2>/dev/null || true)
    manifest_preflight_path=$(awk -F= '$1 == "preflight_path" { count++; value=$2 } END { if (count != 1) exit 1; print value }' "$CLEAN_REMOVE_MANIFEST" 2>/dev/null || true)
    is_sha40 "$manifest_candidate_sha" || { die 'provisioned clean-remove manifest candidate SHA is invalid'; return 1; }
    is_sha256 "$manifest_cleaner_sha256" || { die 'provisioned clean-remove manifest cleaner hash is invalid'; return 1; }
    is_sha256 "$manifest_preflight_sha256" || { die 'provisioned clean-remove manifest preflight hash is invalid'; return 1; }
    [ "$manifest_cleaner_path" = "$CLEAN_REMOVE_ROOT" ] || { die 'provisioned clean-remove manifest cleaner path is invalid'; return 1; }
    [ "$manifest_preflight_path" = "$CLEAN_REMOVE_PREFLIGHT" ] || { die 'provisioned clean-remove manifest preflight path is invalid'; return 1; }
    awk \
        -v candidate_sha="$manifest_candidate_sha" \
        -v cleaner_sha256="$manifest_cleaner_sha256" \
        -v preflight_sha256="$manifest_preflight_sha256" \
        -v cleaner_path="$manifest_cleaner_path" \
        -v preflight_path="$manifest_preflight_path" \
        'NR == 1 { valid = ($0 == "candidate_sha=" candidate_sha); next }
         NR == 2 { valid = valid && ($0 == "cleaner_sha256=" cleaner_sha256); next }
         NR == 3 { valid = valid && ($0 == "preflight_sha256=" preflight_sha256); next }
         NR == 4 { valid = valid && ($0 == "cleaner_path=" cleaner_path); next }
         NR == 5 { valid = valid && ($0 == "preflight_path=" preflight_path); next }
         { valid = 0 }
         END { exit (NR == 5 && valid) ? 0 : 1 }' \
        "$CLEAN_REMOVE_MANIFEST" >/dev/null 2>&1 \
        || { die 'provisioned clean-remove manifest format is invalid'; return 1; }
}

require_fixed_root_provision() {
    self_path=$(readlink -f -- "$0" 2>/dev/null || true)
    [ "$self_path" = "$CLEAN_REMOVE_ROOT" ] \
        || { die "must be provisioned and invoked from $CLEAN_REMOVE_ROOT"; return 1; }
    [ -f "$CLEAN_REMOVE_ROOT" ] && [ ! -L "$CLEAN_REMOVE_ROOT" ] \
        || { die 'provisioned clean-remove script is unsafe'; return 1; }
    [ "$(stat -c '%u:%g:%a' "$CLEAN_REMOVE_ROOT" 2>/dev/null || true)" = '0:0:700' ] \
        || { die 'provisioned clean-remove script must be root:root mode 0700'; return 1; }
    [ -f "$CLEAN_REMOVE_MANIFEST" ] && [ ! -L "$CLEAN_REMOVE_MANIFEST" ] \
        || { die 'provisioned clean-remove manifest is unsafe'; return 1; }
    [ "$(stat -c '%u:%g:%a' "$CLEAN_REMOVE_MANIFEST" 2>/dev/null || true)" = '0:0:600' ] \
        || { die 'provisioned clean-remove manifest must be root:root mode 0600'; return 1; }
    [ -f "$CLEAN_REMOVE_PREFLIGHT" ] && [ ! -L "$CLEAN_REMOVE_PREFLIGHT" ] \
        || { die 'provisioned clean-remove preflight is unsafe'; return 1; }
    [ "$(stat -c '%u:%g:%a' "$CLEAN_REMOVE_PREFLIGHT" 2>/dev/null || true)" = '0:0:755' ] \
        || { die 'provisioned clean-remove preflight must be root:root mode 0755'; return 1; }
    read_strict_manifest || return 1
    [ "$(sha256sum "$CLEAN_REMOVE_ROOT" | awk '{print $1}')" = "$manifest_cleaner_sha256" ] \
        || { die 'provisioned clean-remove script hash does not match its manifest'; return 1; }
    [ "$(sha256sum "$CLEAN_REMOVE_PREFLIGHT" | awk '{print $1}')" = "$manifest_preflight_sha256" ] \
        || { die 'provisioned clean-remove preflight hash does not match its manifest'; return 1; }
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

safe_user_private_file() {
    path=$1; uid=$2; label=$3
    [ -f "$path" ] && [ ! -L "$path" ] || die "$label is unsafe" || return 1
    [ "$(stat -c '%u:%a' "$path" 2>/dev/null || true)" = "$uid:600" ] \
        || die "$label must be install-user-owned mode 0600" || return 1
}

validate_exact_private_members() {
    directory=$1; uid=$2; label=$3
    shift 3
    for member_path in "$directory"/* "$directory"/.[!.]* "$directory"/..?*; do
        [ -e "$member_path" ] || [ -L "$member_path" ] || continue
        member_name=${member_path##*/}
        found=0
        for expected_name in "$@"; do
            [ "$member_name" = "$expected_name" ] && { found=1; break; }
        done
        [ "$found" -eq 1 ] || { die "$label has an unexpected or unsafe member: $member_name"; return 1; }
    done
    for expected_name in "$@"; do
        safe_user_private_file "$directory/$expected_name" "$uid" "$label member $expected_name" || return 1
    done
}

validate_exact_pending_topology() {
    uid=$(id -u "$INSTALL_USER") || return 1
    validate_exact_private_members "$VAULT_DIR" "$uid" 'clean-install vault' archive.zip entry.json || return 1
    validate_exact_private_members "$HANDOFF_DIR" "$uid" 'clean-install handoff directory' handoff.json || return 1
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

validate_release_gates_directory() {
    release_gates_dir=/var/lib/gp-control-plane/release-gates
    [ ! -e "$release_gates_dir" ] && [ ! -L "$release_gates_dir" ] && return 0
    [ -d "$release_gates_dir" ] && [ ! -L "$release_gates_dir" ] || { die "legacy release-gates directory is unsafe: $release_gates_dir"; return 1; }
    [ "$(readlink -f -- "$release_gates_dir" 2>/dev/null || true)" = "$release_gates_dir" ] \
        || { die "legacy release-gates directory is non-canonical: $release_gates_dir"; return 1; }
    [ "$(stat -c '%u:%g:%a' "$release_gates_dir" 2>/dev/null || true)" = "0:$INSTALL_GID:750" ] \
        || { die "legacy release-gates directory must be root:$INSTALL_GID mode 0750: $release_gates_dir"; return 1; }
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
            "$helper_dir/gp-root-helper"|"$helper_dir/gp-clean-remove-root"|"$helper_dir/gp-clean-remove-preflight"|"$helper_dir/gp-clean-remove-root.manifest") ;;
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
    validate_release_gates_directory || return 1
}

validate_fixed_paths() {
    uid=$(id -u "$INSTALL_USER") || return 1
    INSTALL_GID=$(id -g "$INSTALL_USER") || return 1
    [ "$INSTALL_USER" != root ] || { die 'root is not a supported install user'; return 1; }
    case "$INSTALL_GID" in ''|0|0*|*[!0-9]*) die 'install-user group must be a nonzero numeric GID'; return 1 ;; esac
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
    VAULT_ARCHIVE="$VAULT_DIR/archive.zip"
    VAULT_ENTRY="$VAULT_DIR/entry.json"
    HANDOFF_DIR="$TARGET_HOME/.local/share/gp-control-plane/clean-install-handoff"
    HANDOFF_FILE="$HANDOFF_DIR/handoff.json"

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
    safe_user_private_file "$VAULT_ARCHIVE" "$uid" 'clean-install vault archive' || return 1
    safe_user_private_file "$VAULT_ENTRY" "$uid" 'clean-install vault entry' || return 1
    safe_user_private_directory "$HANDOFF_DIR" "$uid" 'clean-install handoff directory' || return 1
    safe_user_private_file "$HANDOFF_FILE" "$uid" 'clean-install handoff file' || return 1
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
    safe_user_private_file "$VAULT_ARCHIVE" "$uid" 'clean-install vault archive after lock' || return 1
    safe_user_private_file "$VAULT_ENTRY" "$uid" 'clean-install vault entry after lock' || return 1
    safe_user_private_directory "$HANDOFF_DIR" "$uid" 'clean-install handoff directory after lock' || return 1
    safe_user_private_file "$HANDOFF_FILE" "$uid" 'clean-install handoff file after lock' || return 1
}

run_final_unprivileged_preflight() {
    runuser -u "$INSTALL_USER" -- "$CLEAN_REMOVE_PREFLIGHT" --install-user "$INSTALL_USER" \
        || { die 'install-user final clean-remove preflight failed'; return 1; }
}

run_preclean_flow() {
    # The fixed preflight must retain normal install-user ownership of HOME and
    # its vault files.  Parent locks are a later root-only TOCTOU boundary and
    # are never acquired when this content validation fails.
    validate_fixed_paths || return 1
    validate_exact_pending_topology || return 1
    validate_removal_surface || return 1
    run_final_unprivileged_preflight || return 1
    acquire_parent_locks || return 1
    revalidate_locked_fixed_paths || return 1
    validate_exact_pending_topology || return 1
    remove_old_gp_surface
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
    rm -f -- /usr/local/libexec/gp-control-plane/gp-clean-remove-preflight || return 1
    rm -f -- /usr/local/libexec/gp-control-plane/gp-clean-remove-root.manifest || return 1
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

if run_preclean_flow; then
    printf '%s\n' 'status=success phase=clean-remove'
    exit 0
fi
if [ "$DESTRUCTIVE_PHASE" = 0 ]; then
    printf '%s\n' 'status=failed phase=pre-clean' >&2
else
    printf '%s\n' 'status=failed phase=clean-remove' >&2
fi
exit 126
