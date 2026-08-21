#!/bin/sh
# Root-owned transaction runner for the device-local clean-install protocol.
# It is copied out of the pinned candidate before the installed helper can be
# replaced.  No caller supplied path, environment or shell fragment is read.
set -u
umask 077
PATH='/usr/sbin:/usr/bin:/sbin:/bin'
export PATH

AUTHORIZATION='/run/gp-control-plane/trusted-clean-install.authorized'
TERMINAL_WRITTEN=0
ROLLBACK_ATTEMPTED=0
ROLLBACK_IN_PROGRESS=0
PUBLISHED=0
PARENT_LOCKS_HELD=0
PARENT_LOCKS_FILE=''
PARENT_LOCK_RELEASE_IN_PROGRESS=0
DEFERRED_SIGNAL=''
SIGNAL_HANDLER_ACTIVE=0
COMMITTED=0
WEB_QUIESCED=0
CORE_QUIESCED=0
WEB_RESTORE_REQUIRED=0
CORE_RESTORE_REQUIRED=0

die() { printf 'clean-install-root-runner: %s\n' "$1" >&2; return 126; }

write_terminal() {
    [ "$TERMINAL_WRITTEN" = 0 ] || return 0
    TERMINAL_WRITTEN=1
    printf '%s\n' "$1" > "$TXN/result" || return 1
    printf '%s\n' "$1"
}

safe_root_directory() {
    path=$1
    label=$2
    [ -d "$path" ] && [ ! -L "$path" ] || die "$label is not a directory" || return 1
    [ "$(readlink -f -- "$path" 2>/dev/null || true)" = "$path" ] || die "$label is non-canonical" || return 1
    [ "$(stat -c '%u:%a' "$path" 2>/dev/null || true)" = '0:700' ] || die "$label must be root-owned mode 0700" || return 1
}

safe_root_file() {
    path=$1
    label=$2
    [ -f "$path" ] && [ ! -L "$path" ] || die "$label is not a regular file" || return 1
    [ "$(readlink -f -- "$path" 2>/dev/null || true)" = "$path" ] || die "$label is non-canonical" || return 1
    [ "$(stat -c '%u:%a' "$path" 2>/dev/null || true)" = '0:700' ] || die "$label must be root-owned mode 0700" || return 1
}

safe_stage_file() {
    path=$1
    label=$2
    [ -f "$path" ] && [ ! -L "$path" ] || die "$label is not a regular file" || return 1
    [ "$(readlink -f -- "$path" 2>/dev/null || true)" = "$path" ] || die "$label is non-canonical" || return 1
    [ "$(stat -c '%u' "$path" 2>/dev/null || true)" = 0 ] || die "$label is not root-owned" || return 1
    case "$(stat -c '%a' "$path" 2>/dev/null || true)" in
        ???|????) ;;
        *) die "$label mode is invalid"; return 1 ;;
    esac
    # Refuse files writable by group or other; user-write is allowed for the
    # root-owned stage fetched by the helper.
    mode=$(stat -c '%a' "$path" 2>/dev/null || true)
    case "$mode" in ??[2367]|?[2367]?|[2367]???) die "$label is group/world-writable"; return 1 ;; esac
}

safe_user_directory() {
    path=$1
    uid=$2
    label=$3
    [ -d "$path" ] && [ ! -L "$path" ] || die "$label is unsafe" || return 1
    [ "$(readlink -f -- "$path" 2>/dev/null || true)" = "$path" ] || die "$label is non-canonical" || return 1
    [ "$(stat -c '%u:%a' "$path" 2>/dev/null || true)" = "$uid:700" ] || die "$label has unsafe owner or mode" || return 1
}

validate_vault() {
    uid=$(id -u "$INSTALL_USER") || return 1
    safe_user_directory "$VAULT_DIR" "$uid" 'clean-install vault' || return 1
    for item in "$VAULT_DIR"/* "$VAULT_DIR"/.[!.]* "$VAULT_DIR"/..?*; do
        [ -e "$item" ] || [ -L "$item" ] || continue
        case "${item##*/}" in archive.zip|entry.json|cleanup.journal.json) ;; *) die 'clean-install vault contains an unexpected member'; return 1 ;; esac
    done
    for item in "$VAULT_DIR/archive.zip" "$VAULT_DIR/entry.json"; do
        [ -f "$item" ] && [ ! -L "$item" ] || die 'clean-install vault is incomplete'; return 1
        [ "$(stat -c '%u:%a' "$item" 2>/dev/null || true)" = "$uid:600" ] || die 'clean-install vault file has unsafe owner or mode'; return 1
    done
}

# The vault is user-owned data that must never enter the transactional managed
# surface.  Check the two dedicated vault parents as well: they are the narrow
# persistence boundary, rather than the whole install-user home directory.
safe_vault_boundary_directory() {
    path=$1
    uid=$2
    label=$3
    [ -d "$path" ] && [ ! -L "$path" ] || die "$label is unsafe" || return 1
    [ "$(readlink -f -- "$path" 2>/dev/null || true)" = "$path" ] || die "$label is non-canonical" || return 1
    [ "$(stat -c '%u' "$path" 2>/dev/null || true)" = "$uid" ] || die "$label owner changed" || return 1
    mode=$(stat -c '%a' "$path" 2>/dev/null || true)
    case "$mode" in
        *[2367]|*[2367]?) die "$label is group/world-writable"; return 1 ;;
    esac
}

safe_managed_boundary_directory() {
    path=$1
    uid=$2
    label=$3
    case "$path" in /*) ;; *) die "$label is not absolute"; return 1 ;; esac
    [ -d "$path" ] && [ ! -L "$path" ] || die "$label is unsafe" || return 1
    [ "$(readlink -f -- "$path" 2>/dev/null || true)" = "$path" ] || die "$label is non-canonical" || return 1
    [ "$(stat -c '%u' "$path" 2>/dev/null || true)" = "$uid" ] || die "$label owner changed" || return 1
    parent=$(dirname -- "$path")
    [ -d "$parent" ] && [ ! -L "$parent" ] || die "$label parent is unsafe" || return 1
    [ "$(readlink -f -- "$parent" 2>/dev/null || true)" = "$parent" ] || die "$label parent is non-canonical" || return 1
}

paths_overlap() {
    left=$1
    right=$2
    case "$left" in "$right"|"$right"/*) return 0 ;; esac
    case "$right" in "$left"|"$left"/*) return 0 ;; esac
    return 1
}

validate_transaction_boundaries() {
    uid=$(id -u "$INSTALL_USER") || return 1
    vault_store=$(dirname -- "$VAULT_DIR")
    vault_state_root=$(dirname -- "$vault_store")
    safe_vault_boundary_directory "$vault_store" "$uid" 'clean-install vault store' || return 1
    safe_vault_boundary_directory "$vault_state_root" "$uid" 'clean-install vault state root' || return 1
    safe_managed_boundary_directory "$INSTALL_DIR" "$uid" 'managed install directory' || return 1
    safe_managed_boundary_directory "$STATE_DIR" "$uid" 'managed state directory' || return 1
    for protected in "$VAULT_DIR" "$vault_store" "$vault_state_root"; do
        if paths_overlap "$INSTALL_DIR" "$protected"; then
            die 'managed install directory overlaps the device-local vault boundary'
            return 1
        fi
        if paths_overlap "$STATE_DIR" "$protected"; then
            die 'managed state directory overlaps the device-local vault boundary'
            return 1
        fi
    done
}

record_and_lock_parent() {
    path=$1
    uid=$2
    if awk -F'|' -v wanted="$path" '$1 == wanted { found=1 } END { exit found ? 0 : 1 }' "$PARENT_LOCKS_FILE" 2>/dev/null; then
        return 0
    fi
    [ -d "$path" ] && [ ! -L "$path" ] || die 'managed parent is unsafe' || return 1
    [ "$(readlink -f -- "$path" 2>/dev/null || true)" = "$path" ] || die 'managed parent is non-canonical' || return 1
    [ "$(stat -c '%u' "$path" 2>/dev/null || true)" = "$uid" ] || die 'managed parent owner changed' || return 1
    parent_uid=$(stat -c '%u' "$path") || return 1
    parent_gid=$(stat -c '%g' "$path") || return 1
    parent_mode=$(stat -c '%a' "$path") || return 1
    printf '%s|%s|%s|%s\n' "$path" "$parent_uid" "$parent_gid" "$parent_mode" >> "$PARENT_LOCKS_FILE" || return 1
    chown root:root "$path" || return 1
    chmod 0711 "$path" || return 1
    [ "$(stat -c '%u:%g:%a' "$path" 2>/dev/null || true)" = '0:0:711' ] || return 1
}

acquire_parent_locks() {
    uid=$(id -u "$INSTALL_USER") || return 1
    target_home=$(getent passwd "$INSTALL_USER" | cut -d: -f6) || return 1
    [ -n "$target_home" ] || { die 'cannot resolve install-user home'; return 1; }
    [ "$target_home" = "$(readlink -f -- "$target_home" 2>/dev/null || true)" ] || { die 'install-user home is non-canonical'; return 1; }
    PARENT_LOCKS_FILE="$TXN/parent-locks.records"
    : > "$PARENT_LOCKS_FILE" || return 1
    chmod 0600 "$PARENT_LOCKS_FILE" || return 1
    PARENT_LOCKS_HELD=1
    record_and_lock_parent "$target_home" "$uid" || return 1
    record_and_lock_parent "$(dirname -- "$INSTALL_DIR")" "$uid" || return 1
    if [ "$STATE_DIR" != "$INSTALL_DIR" ] && [ "${STATE_DIR#"$INSTALL_DIR"/}" = "$STATE_DIR" ]; then
        record_and_lock_parent "$(dirname -- "$STATE_DIR")" "$uid" || return 1
    fi
}

revalidate_parent_locks() {
    [ "$PARENT_LOCKS_HELD" = 1 ] || { die 'managed parent locks are not held'; return 1; }
    while IFS='|' read -r path saved_uid saved_gid saved_mode; do
        [ -n "$path" ] || continue
        [ -d "$path" ] && [ ! -L "$path" ] || { die 'managed parent lock path changed'; return 1; }
        [ "$(readlink -f -- "$path" 2>/dev/null || true)" = "$path" ] || { die 'managed parent lock path is non-canonical'; return 1; }
        [ "$(stat -c '%u:%g:%a' "$path" 2>/dev/null || true)" = '0:0:711' ] || { die 'managed parent lock was changed'; return 1; }
    done < "$PARENT_LOCKS_FILE"
    validate_vault || return 1
    for protected in "$VAULT_DIR" "$(dirname -- "$VAULT_DIR")" "$(dirname -- "$(dirname -- "$VAULT_DIR")")"; do
        paths_overlap "$INSTALL_DIR" "$protected" && { die 'managed install directory overlaps the device-local vault boundary'; return 1; }
        paths_overlap "$STATE_DIR" "$protected" && { die 'managed state directory overlaps the device-local vault boundary'; return 1; }
    done
}

release_parent_locks() {
    [ "$PARENT_LOCKS_HELD" = 1 ] || return 0
    PARENT_LOCK_RELEASE_IN_PROGRESS=1
    while IFS='|' read -r path saved_uid saved_gid saved_mode; do
        [ -n "$path" ] || continue
        if ! [ -d "$path" ] || [ -L "$path" ]; then
            PARENT_LOCK_RELEASE_IN_PROGRESS=0
            return 1
        fi
        if ! chown "$saved_uid:$saved_gid" "$path"; then
            PARENT_LOCK_RELEASE_IN_PROGRESS=0
            return 1
        fi
        if ! chmod "$saved_mode" "$path"; then
            PARENT_LOCK_RELEASE_IN_PROGRESS=0
            return 1
        fi
    done < "$PARENT_LOCKS_FILE"
    PARENT_LOCKS_HELD=0
    PARENT_LOCK_RELEASE_IN_PROGRESS=0
}

reacquire_parent_locks_for_rollback() {
    [ "$PARENT_LOCKS_HELD" = 0 ] || return 0
    validate_transaction_boundaries || return 1
    acquire_parent_locks || return 1
    revalidate_parent_locks || return 1
}

resume_deferred_signal() {
    [ -n "$DEFERRED_SIGNAL" ] || return 0
    signal=$DEFERRED_SIGNAL
    DEFERRED_SIGNAL=''
    on_signal "$signal"
}

validate_authorization() {
    [ -f "$AUTHORIZATION" ] && [ ! -L "$AUTHORIZATION" ] || die 'trusted clean-install authorization is missing' || return 1
    [ "$(stat -c '%u:%g:%a' "$AUTHORIZATION" 2>/dev/null || true)" = '0:0:600' ] || die 'trusted clean-install authorization is unsafe' || return 1
    [ "$(cat "$AUTHORIZATION")" = "trusted-clean-install-v1 $STAGE_DIR" ] || die 'trusted clean-install authorization does not bind staged candidate' || return 1
}

snapshot_file() {
    source=$1
    name=$2
    [ ! -e "$source" ] && [ ! -L "$source" ] && return 0
    [ -f "$source" ] && [ ! -L "$source" ] || die "managed root file is unsafe: $source" || return 1
    cp -a -- "$source" "$ROLLBACK_ROOT/$name" || return 1
    printf '%s\n' "$source" >> "$ROLLBACK_ROOT/files.list" || return 1
}

snapshot_unit_path() {
    source=$1
    name=$2
    [ ! -e "$source" ] && [ ! -L "$source" ] && return 0
    if [ -L "$source" ]; then
        [ "$(readlink -- "$source" 2>/dev/null || true)" = /dev/null ] || die "managed systemd unit symlink is unsafe: $source" || return 1
        [ "$(stat -c '%u' "$source" 2>/dev/null || true)" = 0 ] || die "managed systemd unit mask is not root-owned: $source" || return 1
        printf '/dev/null\n' > "$ROLLBACK_ROOT/$name.mask" || return 1
        return 0
    fi
    snapshot_file "$source" "$name"
}

snapshot_systemd_unit() {
    unit=$1
    name=$2
    snapshot_unit_path "/etc/systemd/system/$unit" "$name" || return 1
    snapshot_unit_path "/run/systemd/system/$unit" "$name.runtime" || return 1
}

snapshot_root_surface() {
    install -d -m 0700 -o root -g root "$ROLLBACK_ROOT" || return 1
    : > "$ROLLBACK_ROOT/files.list" || return 1
    snapshot_file /usr/local/libexec/gp-control-plane/gp-root-helper root-helper || return 1
    snapshot_file /etc/default/gp-control-plane-root-helper root-helper-config || return 1
    snapshot_file /etc/sudoers.d/gp-control-plane-root-helper sudoers || return 1
    snapshot_file /etc/default/gp-control-plane-install-profile install-profile || return 1
    snapshot_file /etc/default/gp-control-plane-core core-env || return 1
    snapshot_file /etc/default/gp-control-plane-web web-env || return 1
    snapshot_systemd_unit gp-control-plane-core.service core-unit || return 1
    snapshot_systemd_unit gp-control-plane-web.service web-unit || return 1
    snapshot_service_topology gp-control-plane-core.service core || return 1
    snapshot_service_topology gp-control-plane-web.service web || return 1
}

systemctl_property() {
    unit=$1
    property=$2
    value=$(systemctl show --property="$property" --value "$unit" 2>/dev/null) || {
        die "cannot query $property for $unit"
        return 1
    }
    case "$value" in *'
'*) die "ambiguous $property for $unit"; return 1 ;; esac
    printf '%s\n' "$value"
}

snapshot_service_topology() {
    unit=$1
    name=$2
    state_file="$ROLLBACK_ROOT/$name.service-state"
    load_state=$(systemctl_property "$unit" LoadState) || return 1
    case "$load_state" in
        not-found)
            printf 'load=not-found\n' > "$state_file" || return 1
            return 0
            ;;
        loaded) ;;
        *) die "unexpected LoadState for $unit: $load_state"; return 1 ;;
    esac
    active_state=$(systemctl_property "$unit" ActiveState) || return 1
    case "$active_state" in active|inactive|failed|activating|deactivating) ;; *) die "unexpected ActiveState for $unit: $active_state"; return 1 ;; esac
    enabled_state=$(systemctl is-enabled "$unit" 2>/dev/null || true)
    case "$enabled_state" in
        enabled|enabled-runtime|disabled|disabled-runtime|static|indirect|generated|transient|alias|linked|linked-runtime|masked|masked-runtime) ;;
        *) die "cannot query UnitFileState for $unit"; return 1 ;;
    esac
    fragment_path=$(systemctl_property "$unit" FragmentPath) || return 1
    case "$enabled_state" in
        masked|masked-runtime)
            [ "$fragment_path" = /dev/null ] || die "masked $unit is not controlled by /dev/null" || return 1
            [ "$active_state" != active ] || die "masked $unit is unexpectedly active" || return 1
            ;;
    esac
    {
        printf 'load=%s\n' "$load_state"
        printf 'active=%s\n' "$active_state"
        printf 'enabled=%s\n' "$enabled_state"
        printf 'fragment=%s\n' "$fragment_path"
    } > "$state_file" || return 1
}

service_state_value() {
    file=$1
    key=$2
    value=$(awk -F= -v wanted="$key" '$1 == wanted { print substr($0, length(wanted) + 2); found=1 } END { exit found ? 0 : 1 }' "$file") || return 1
    case "$value" in *'
'*) return 1 ;; esac
    printf '%s\n' "$value"
}

quiesce_services() {
    quiesce_service gp-control-plane-web.service web || return 1
    quiesce_service gp-control-plane-core.service core || return 1
}

quiesce_service() {
    unit=$1
    name=$2
    state_file="$ROLLBACK_ROOT/$name.service-state"
    load_state=$(service_state_value "$state_file" load) || { die "missing saved topology for $unit"; return 1; }
    [ "$load_state" = not-found ] && return 0
    [ "$load_state" = loaded ] || { die "unsafe saved topology for $unit"; return 1; }
    active_state=$(service_state_value "$state_file" active) || { die "missing saved activity for $unit"; return 1; }
    enabled_state=$(service_state_value "$state_file" enabled) || { die "missing saved enablement for $unit"; return 1; }
    [ "$active_state" = active ] || return 0
    case "$enabled_state" in masked|masked-runtime) die "refusing to stop masked $unit"; return 1 ;; esac
    # This intent is durable in the transaction shell before stop(1).  A
    # signal, stop error, or post-stop query failure can therefore never make
    # rollback forget an originally active unit that may already be down.
    case "$name" in web) WEB_RESTORE_REQUIRED=1 ;; core) CORE_RESTORE_REQUIRED=1 ;; esac
    systemctl stop "$unit" || return 1
    current_state=$(systemctl_property "$unit" ActiveState) || return 1
    [ "$current_state" != active ] || return 1
    case "$name" in web) WEB_QUIESCED=1 ;; core) CORE_QUIESCED=1 ;; esac
}

# Rollback runs after the candidate may have created units that did not exist
# before the transaction.  Inspect the *current* topology rather than blindly
# stopping a historical Web unit: absent and masked-inactive units are left
# alone, but a candidate-created active unit is quiesced before its files move.
quiesce_rollback_services() {
    quiesce_current_service gp-control-plane-web.service || return 1
    quiesce_current_service gp-control-plane-core.service || return 1
}

quiesce_current_service() {
    unit=$1
    load_state=$(systemctl_property "$unit" LoadState) || return 1
    [ "$load_state" = not-found ] && return 0
    [ "$load_state" = loaded ] || { die "unexpected current LoadState for $unit: $load_state"; return 1; }
    active_state=$(systemctl_property "$unit" ActiveState) || return 1
    [ "$active_state" = active ] || return 0
    enabled_state=$(systemctl is-enabled "$unit" 2>/dev/null || true)
    case "$enabled_state" in
        masked|masked-runtime) die "refusing to stop currently masked $unit"; return 1 ;;
        enabled|enabled-runtime|disabled|disabled-runtime|static|indirect|generated|transient|alias|linked|linked-runtime) ;;
        *) die "cannot query current UnitFileState for $unit"; return 1 ;;
    esac
    systemctl stop "$unit" || return 1
    current_state=$(systemctl_property "$unit" ActiveState) || return 1
    [ "$current_state" != active ] || return 1
}

snapshot_managed_directories() {
    revalidate_parent_locks || return 1
    [ -d "$INSTALL_DIR" ] && [ ! -L "$INSTALL_DIR" ] || die 'managed install directory is unsafe' || return 1
    [ "$(readlink -f -- "$INSTALL_DIR" 2>/dev/null || true)" = "$INSTALL_DIR" ] || die 'managed install directory is non-canonical' || return 1
    install_parent=$(dirname -- "$INSTALL_DIR")
    [ -d "$install_parent" ] && [ ! -L "$install_parent" ] || die 'managed install parent is unsafe' || return 1
    mv -- "$INSTALL_DIR" "$ROLLBACK_ROOT/install-dir" || return 1
    PUBLISHED=1
    if [ "$STATE_DIR" != "$INSTALL_DIR" ] && [ "${STATE_DIR#"$INSTALL_DIR"/}" = "$STATE_DIR" ]; then
        [ -d "$STATE_DIR" ] && [ ! -L "$STATE_DIR" ] || die 'managed state directory is unsafe' || return 1
        [ "$(readlink -f -- "$STATE_DIR" 2>/dev/null || true)" = "$STATE_DIR" ] || die 'managed state directory is non-canonical' || return 1
        mv -- "$STATE_DIR" "$ROLLBACK_ROOT/state-dir" || return 1
        STATE_SEPARATE=1
    else
        STATE_SEPARATE=0
    fi
}

prepare_candidate_state_directory() {
    [ "${STATE_SEPARATE:-0}" = 1 ] || return 0
    revalidate_parent_locks || return 1
    uid=$(id -u "$INSTALL_USER") || return 1
    gid=$(id -g "$INSTALL_USER") || return 1
    [ ! -e "$STATE_DIR" ] && [ ! -L "$STATE_DIR" ] || return 1
    install -d -m 0700 -o "$uid" -g "$gid" "$STATE_DIR" || return 1
}

restore_root_surface() {
    [ -d "$ROLLBACK_ROOT" ] && [ ! -L "$ROLLBACK_ROOT" ] || return 1
    # Explicit root-managed whitelist; no user-derived destination is restored.
    for pair in \
        'root-helper:/usr/local/libexec/gp-control-plane/gp-root-helper' \
        'root-helper-config:/etc/default/gp-control-plane-root-helper' \
        'sudoers:/etc/sudoers.d/gp-control-plane-root-helper' \
        'install-profile:/etc/default/gp-control-plane-install-profile' \
        'core-env:/etc/default/gp-control-plane-core' \
        'web-env:/etc/default/gp-control-plane-web'; do
        name=${pair%%:*}; target=${pair#*:}
        if [ -f "$ROLLBACK_ROOT/$name" ]; then
            install -d -m 0755 -o root -g root "$(dirname -- "$target")" || return 1
            install -m 0600 -o root -g root "$ROLLBACK_ROOT/$name" "$target" || return 1
        else
            [ ! -e "$target" ] && [ ! -L "$target" ] || rm -f -- "$target" || return 1
        fi
    done
    restore_systemd_unit gp-control-plane-core.service core-unit || return 1
    restore_systemd_unit gp-control-plane-web.service web-unit || return 1
    systemctl daemon-reload || return 1
}

restore_unit_path() {
    target=$1
    name=$2
    if [ -f "$ROLLBACK_ROOT/$name" ]; then
        install -d -m 0755 -o root -g root "$(dirname -- "$target")" || return 1
        [ ! -e "$target" ] && [ ! -L "$target" ] || rm -f -- "$target" || return 1
        install -m 0600 -o root -g root "$ROLLBACK_ROOT/$name" "$target" || return 1
    elif [ -f "$ROLLBACK_ROOT/$name.mask" ]; then
        [ "$(cat "$ROLLBACK_ROOT/$name.mask")" = /dev/null ] || { die "unsafe saved systemd mask: $target"; return 1; }
        install -d -m 0755 -o root -g root "$(dirname -- "$target")" || return 1
        [ ! -e "$target" ] && [ ! -L "$target" ] || rm -f -- "$target" || return 1
        ln -s /dev/null "$target" || return 1
    else
        [ ! -e "$target" ] && [ ! -L "$target" ] || rm -f -- "$target" || return 1
    fi
}

restore_systemd_unit() {
    unit=$1
    name=$2
    restore_unit_path "/etc/systemd/system/$unit" "$name" || return 1
    restore_unit_path "/run/systemd/system/$unit" "$name.runtime" || return 1
}

restore_service_topology() {
    unit=$1
    name=$2
    state_file="$ROLLBACK_ROOT/$name.service-state"
    load_state=$(service_state_value "$state_file" load) || { die "missing saved topology for $unit"; return 1; }
    [ "$load_state" = not-found ] && return 0
    [ "$load_state" = loaded ] || { die "unsafe saved topology for $unit"; return 1; }
    enabled_state=$(service_state_value "$state_file" enabled) || { die "missing saved enablement for $unit"; return 1; }
    active_state=$(service_state_value "$state_file" active) || { die "missing saved activity for $unit"; return 1; }
    fragment_path=$(service_state_value "$state_file" fragment) || { die "missing saved fragment for $unit"; return 1; }
    systemctl unmask "$unit" >/dev/null 2>&1 || return 1
    systemctl disable "$unit" >/dev/null 2>&1 || return 1
    systemctl disable --runtime "$unit" >/dev/null 2>&1 || return 1
    case "$enabled_state" in
        enabled|linked) systemctl enable "$unit" || return 1 ;;
        enabled-runtime|linked-runtime) systemctl enable --runtime "$unit" || return 1 ;;
        disabled) ;;
        disabled-runtime) systemctl disable --runtime "$unit" || return 1 ;;
        static|indirect|generated|transient|alias) ;;
        masked) [ "$fragment_path" = /dev/null ] || { die "unsafe saved mask for $unit"; return 1; }; systemctl mask "$unit" || return 1 ;;
        masked-runtime) [ "$fragment_path" = /dev/null ] || { die "unsafe saved runtime mask for $unit"; return 1; }; systemctl mask --runtime "$unit" || return 1 ;;
        *) die "unsafe saved enablement for $unit"; return 1 ;;
    esac
    [ "$active_state" = active ] || return 0
    case "$enabled_state" in masked|masked-runtime) die "refusing to start masked $unit"; return 1 ;; esac
    systemctl start "$unit" || return 1
}

rollback() {
    [ "$ROLLBACK_ATTEMPTED" = 0 ] || return 0
    [ "${ROLLBACK_IN_PROGRESS:-0}" = 0 ] || return 1
    ROLLBACK_IN_PROGRESS=1
    if rollback_impl; then
        ROLLBACK_IN_PROGRESS=0
        ROLLBACK_ATTEMPTED=1
        resume_deferred_signal
        return 0
    fi
    ROLLBACK_IN_PROGRESS=0
    return 1
}

rollback_impl() {
    if [ "$PUBLISHED" != 1 ]; then
        release_parent_locks || return 1
        restore_quiesced_services || return 1
        return 0
    fi
    reacquire_parent_locks_for_rollback || return 1
    quiesce_rollback_services || return 1
    revalidate_parent_locks || return 1
    [ ! -e "$INSTALL_DIR" ] && [ ! -L "$INSTALL_DIR" ] || rm -rf --one-file-system "$INSTALL_DIR" || return 1
    mv -- "$ROLLBACK_ROOT/install-dir" "$INSTALL_DIR" || return 1
    if [ "${STATE_SEPARATE:-0}" = 1 ]; then
        revalidate_parent_locks || return 1
        [ ! -e "$STATE_DIR" ] && [ ! -L "$STATE_DIR" ] || rm -rf --one-file-system "$STATE_DIR" || return 1
        mv -- "$ROLLBACK_ROOT/state-dir" "$STATE_DIR" || return 1
    fi
    restore_root_surface || return 1
    release_parent_locks || return 1
    restore_service_topology gp-control-plane-core.service core || return 1
    restore_service_topology gp-control-plane-web.service web || return 1
}

restore_quiesced_services() {
    [ "$CORE_RESTORE_REQUIRED" = 0 ] || restore_service_topology gp-control-plane-core.service core || return 1
    [ "$WEB_RESTORE_REQUIRED" = 0 ] || restore_service_topology gp-control-plane-web.service web || return 1
}

target_profile_web_enabled() {
    profile=/etc/default/gp-control-plane-install-profile
    [ -f "$profile" ] && [ ! -L "$profile" ] || return 1
    [ "$(stat -c '%u:%g:%a' "$profile" 2>/dev/null || true)" = '0:0:600' ] || return 1
    value=$(awk -F= '$1 == "GP_INSTALL_WEB" { print substr($0, length($1) + 2); found=1 } END { exit found ? 0 : 1 }' "$profile") || return 1
    case "$value" in "'on'"|on) return 0 ;; "'off'"|off) return 1 ;; *) return 2 ;; esac
}

activate_target_services_after_unlock() {
    quiesce_rollback_services || return 1
    release_parent_locks || return 1
    resume_deferred_signal
    systemctl daemon-reload || return 1
    systemctl enable gp-control-plane-core.service || return 1
    systemctl start gp-control-plane-core.service || return 1
    systemctl is-active --quiet gp-control-plane-core.service || return 1
    if target_profile_web_enabled; then
        systemctl enable gp-control-plane-web.service || return 1
        systemctl start gp-control-plane-web.service || return 1
        systemctl is-active --quiet gp-control-plane-web.service || return 1
    else
        profile_status=$?
        [ "$profile_status" = 1 ] || return 1
    fi
}

on_signal() {
    signal=$1
    [ "$COMMITTED" = 0 ] || return 0
    if [ "$PARENT_LOCK_RELEASE_IN_PROGRESS" = 1 ] || [ "$SIGNAL_HANDLER_ACTIVE" = 1 ] || [ "$ROLLBACK_IN_PROGRESS" = 1 ]; then
        [ -n "$DEFERRED_SIGNAL" ] || DEFERRED_SIGNAL=$signal
        return 0
    fi
    SIGNAL_HANDLER_ACTIVE=1
    if rollback; then
        write_terminal "status=failed rollback=completed signal=$signal" || true
    else
        write_terminal "status=failed rollback=failed signal=$signal" || true
    fi
    rm -f -- "$AUTHORIZATION"
    exit 126
}

install_signal_traps() {
    trap 'on_signal HUP' HUP
    trap 'on_signal INT' INT
    trap 'on_signal TERM' TERM
}

commit_success() {
    # Success becomes irreversible only when its terminal record has been
    # persisted.  Ignore asynchronous rollback signals across that narrow
    # write so no failed evidence can follow a successful terminal.
    trap '' HUP INT TERM
    COMMITTED=1
    if write_terminal 'status=success rollback=not-required'; then
        return 0
    fi
    COMMITTED=0
    TERMINAL_WRITTEN=0
    install_signal_traps
    return 1
}

cleanup() { rm -f -- "$AUTHORIZATION"; }

write_rollback_terminal() {
    result=$1
    if [ -n "$DEFERRED_SIGNAL" ]; then
        signal=$DEFERRED_SIGNAL
        DEFERRED_SIGNAL=''
        write_terminal "status=failed rollback=$result signal=$signal" || true
    else
        write_terminal "status=failed rollback=$result" || true
    fi
}

main() {
    safe_root_directory "$TXN" 'clean-install transaction' || return 1
    safe_root_directory "$STAGE_DIR" 'staged candidate' || return 1
    safe_root_file "$TXN/runner" 'root-owned staged runner' || return 1
    [ "$(readlink -f -- "$0" 2>/dev/null || true)" = "$TXN/runner" ] || die 'runner must execute only the root-owned transaction copy' || return 1
    safe_stage_file "$STAGE_DIR/scripts/install-linux.sh" 'staged installer' || return 1
    validate_authorization || return 1
    validate_vault || return 1
    validate_transaction_boundaries || return 1
    acquire_parent_locks || return 1
    snapshot_root_surface || return 1
    quiesce_services || return 1
    snapshot_managed_directories || return 1
    prepare_candidate_state_directory || return 1
    revalidate_parent_locks || return 1
    env -i PATH="$PATH" HOME=/root /bin/bash "$STAGE_DIR/scripts/install-linux.sh" --trusted-clean-install || return 1
    activate_target_services_after_unlock || return 1
    commit_success || return 1
    return 0
}

[ "$(id -u)" = 0 ] || { die 'must be executed as root'; exit 126; }
[ "$#" -eq 12 ] || { die 'internal argument count is invalid'; exit 126; }
[ "$1" = --transaction-dir ] && [ "$3" = --stage-dir ] && [ "$5" = --install-dir ] && [ "$7" = --state-dir ] && [ "$9" = --install-user ] && [ "$11" = --vault-dir ] || { die 'internal argument grammar is invalid'; exit 126; }
TXN=$2
STAGE_DIR=$4
INSTALL_DIR=$6
STATE_DIR=$8
INSTALL_USER=$10
VAULT_DIR=$12
ROLLBACK_ROOT="$TXN/rollback"
install_signal_traps
trap cleanup EXIT

if main; then
    exit 0
fi
if rollback; then
    write_rollback_terminal completed
else
    write_rollback_terminal failed
fi
exit 126
