#!/bin/sh
# One authorization installs the verified application and cleans temporary files.
set -eu

target=$1
archive=$2
expected_digest=$3
version=$4
identifier=$5
token=$6
caller_uid=$7
parent_pid=$8
case "$token" in macos-update-startup-*) ;; *) exit 1 ;; esac
case "$token" in *[!a-zA-Z0-9-]*|'') exit 1 ;; esac
case "$caller_uid:$parent_pid" in *[!0-9:]*|:*|*:) exit 1 ;; esac
[ "${#expected_digest}" -eq 64 ]
case "$expected_digest" in *[!0-9a-f]*) exit 1 ;; esac
parent=$(dirname "$target")
identity=$(printf '%s' "$target" | /usr/bin/shasum -a 256 | /usr/bin/awk '{print $1}')
legacy="$parent/.pymss-studio-previous-$identity.app"
control="/private/tmp/pymss-studio-authorized-$token"
umask 077
# The root-owned directory in sticky /private/tmp cannot be replaced by the caller.
/bin/mkdir -m 755 "$control"
/bin/mkdir -m 700 "$control/private"
printf 'preparing' > "$control/status"
/bin/chmod 644 "$control/status"
printf '%64s' '' > "$control/outcome"
/usr/sbin/chown "$caller_uid" "$control/outcome"
/bin/chmod 600 "$control/outcome"
stage=
staged=
phase=preparing

validate_bundle() {
    [ ! -L "$1" ] || return 1
    [ -d "$1" ] || return 1
    [ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$1/Contents/Info.plist")" = "$identifier" ] || return 1
    /usr/bin/codesign --verify --deep --strict "$1"
}

exchange() {
    /usr/bin/osascript -l JavaScript -e 'function run(argv) { ObjC.import("stdio"); return $.renamex_np(argv[0], argv[1], 2) === 0 ? "swapped" : "failed"; }' "$1" "$2"
}

cleanup_legacy_stages() {
    for candidate in "$parent"/.pymss-studio-update-*; do
        [ "$candidate" != "$stage" ] || continue
        [ -d "$candidate" ] && [ ! -L "$candidate" ] || continue
        # A marker protects live updaters, not directories from ended or
        # interrupted transactions. Unknown contents are never deleted.
        known=1
        found=0
        for item in "$candidate"/* "$candidate"/.[!.]* "$candidate"/..?*; do
            [ -e "$item" ] || [ -L "$item" ] || continue
            case "$(basename "$item")" in
                .pymss-update-active)
                    [ -f "$item" ] && [ ! -L "$item" ] || { known=0; continue; }
                    marker_pid=$(/usr/bin/head -c 64 "$item" | /usr/bin/awk '{print $1}')
                    expired=$(/usr/bin/find "$item" -mtime +0 -print)
                    case "$marker_pid" in
                        ''|*[!0-9]*) ;;
                        *) if [ -z "$expired" ] && [ "$marker_pid" != "$$" ] && [ "$marker_pid" != "$parent_pid" ] && /bin/kill -0 "$marker_pid" 2>/dev/null; then known=0; fi ;;
                    esac
                    ;;
                'Pymss Studio.app'|obsolete.app)
                    found=1
                    if ! validate_bundle "$item"; then known=0; fi
                    ;;
                *) known=0 ;;
            esac
        done
        if [ "$known" -eq 1 ] && [ "$found" -eq 1 ]; then
            /bin/rm -rf "$candidate" || echo "Unable to clean a previous update directory: $candidate" >&2
        fi
    done
}

finish() {
    status=$?
    trap - 0 HUP INT TERM
    case "$phase" in
        committed|aborted) ;;
        *) phase=failed; printf 'failed' > "$control/status" || : ;;
    esac
    if [ -n "$stage" ]; then /bin/rm -rf "$stage" || status=1; fi
    /bin/rm -rf "$control/private" || status=1
    if [ "$status" -ne 0 ]; then printf 'failed' > "$control/status" || :; fi
    i=0
    while [ "$i" -lt 25 ]; do
        [ "$(/usr/bin/head -c 64 "$control/outcome" | /usr/bin/awk '{print $1}')" != observed ] || break
        i=$((i + 1)); /bin/sleep 0.2
    done
    /bin/rm -rf "$control" || status=1
    exit "$status"
}
trap finish 0
trap 'exit 1' HUP INT TERM

# Hash and unpack a root-owned snapshot, never the caller's mutable extracted app.
/bin/cp "$archive" "$control/private/update.app.tar.gz"
actual_digest=$(/usr/bin/shasum -a 256 "$control/private/update.app.tar.gz" | /usr/bin/awk '{print $1}')
[ "$actual_digest" = "$expected_digest" ] || { echo 'The verified update archive changed before authorization.' >&2; exit 1; }
/bin/mkdir "$control/private/extracted"
(umask 022; /usr/bin/tar --no-same-owner --no-same-permissions -xzf "$control/private/update.app.tar.gz" -C "$control/private/extracted")
source="$control/private/extracted/Pymss Studio.app"
validate_bundle "$source"
[ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$source/Contents/Info.plist")" = "$version" ]
validate_bundle "$target"
/bin/kill -0 "$parent_pid" 2>/dev/null || exit 1
if [ -e "$legacy" ] || [ -L "$legacy" ]; then
    validate_bundle "$legacy"
    /bin/rm -rf "$legacy"
fi
stage=$(/usr/bin/mktemp -d "$parent/.pymss-studio-update-XXXXXXXX")
printf '%s' "$$" > "$stage/.pymss-update-active"
staged="$stage/Pymss Studio.app"
/usr/bin/ditto "$source" "$staged"
validate_bundle "$staged"

phase=exchanging
result=$(exchange "$staged" "$target") || exit 1
case "$result" in
    swapped) /bin/rm -rf "$staged" ;;
    failed)
        /bin/rm -rf "$target"
        /bin/mv "$staged" "$target"
        ;;
    *) exit 1 ;;
esac
phase=installed
printf 'installed' > "$control/status"

# The caller may write this regular file but cannot replace it with a symlink/FIFO.
i=0
while [ "$i" -lt 600 ]; do
    outcome=$(/usr/bin/head -c 64 "$control/outcome" | /usr/bin/awk '{print $1}')
    case "$outcome" in
        commit)
            cleanup_legacy_stages
            phase=committed
            printf 'committed' > "$control/status"
            exit 0
            ;;
        abort)
            phase=aborted
            printf 'aborted' > "$control/status"
            exit 0
            ;;
        ready:*)
            child_pid=${outcome#ready:}
            case "$child_pid" in ''|*[!0-9]*) exit 1 ;; esac
            if ! /bin/kill -0 "$parent_pid" 2>/dev/null && /bin/kill -0 "$child_pid" 2>/dev/null; then
                cleanup_legacy_stages
                phase=committed
                printf 'committed' > "$control/status"
                exit 0
            fi
            ;;
        '') /bin/kill -0 "$parent_pid" 2>/dev/null || exit 1 ;;
        *) exit 1 ;;
    esac
    i=$((i + 1))
    /bin/sleep 0.2
done

echo 'The application was installed, but startup confirmation timed out.' >&2
exit 1
