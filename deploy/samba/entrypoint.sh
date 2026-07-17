#!/bin/sh
set -eu

MEDIA_PATH=/srv/media
SAMBA_GROUP=fmshare
SAMBA_USER_VALUE=${SAMBA_USER:-}
SAMBA_UID_VALUE=${SAMBA_UID:-1000}
SAMBA_GID_VALUE=${SAMBA_GID:-1000}
SAMBA_PASSWORD_VALUE=${SAMBA_PASSWORD:-}
SAMBA_PASSWORD_FILE_VALUE=${SAMBA_PASSWORD_FILE:-}

fail() {
    echo "samba-entrypoint: $*" >&2
    exit 1
}

case "$SAMBA_USER_VALUE" in
    ''|root|[!a-z_]*|*[!a-z0-9_-]*)
        fail 'SAMBA_USER must use lowercase letters, digits, underscore or hyphen and must not be root'
        ;;
esac
[ "${#SAMBA_USER_VALUE}" -le 32 ] || fail 'SAMBA_USER must be at most 32 characters'

case "$SAMBA_UID_VALUE" in
    ''|*[!0-9]*) fail 'SAMBA_UID must be a positive integer' ;;
esac
case "$SAMBA_GID_VALUE" in
    ''|*[!0-9]*) fail 'SAMBA_GID must be a positive integer' ;;
esac
[ "$SAMBA_UID_VALUE" -gt 0 ] || fail 'SAMBA_UID must be greater than zero'
[ "$SAMBA_GID_VALUE" -gt 0 ] || fail 'SAMBA_GID must be greater than zero'

if [ -n "$SAMBA_PASSWORD_VALUE" ] && [ -n "$SAMBA_PASSWORD_FILE_VALUE" ]; then
    fail 'set only one of SAMBA_PASSWORD and SAMBA_PASSWORD_FILE'
fi
if [ -n "$SAMBA_PASSWORD_FILE_VALUE" ]; then
    [ -r "$SAMBA_PASSWORD_FILE_VALUE" ] || fail 'SAMBA_PASSWORD_FILE is not readable'
    SAMBA_PASSWORD_VALUE=$(sed -e 's/[[:space:]]*$//' "$SAMBA_PASSWORD_FILE_VALUE")
fi
[ -n "$SAMBA_PASSWORD_VALUE" ] || fail 'SAMBA_PASSWORD or SAMBA_PASSWORD_FILE is required'
[ "${#SAMBA_PASSWORD_VALUE}" -ge 12 ] || fail 'the Samba password must be at least 12 characters'

if getent group "$SAMBA_GID_VALUE" >/dev/null 2>&1; then
    existing_group=$(getent group "$SAMBA_GID_VALUE" | cut -d: -f1)
    [ "$existing_group" = "$SAMBA_GROUP" ] || fail "SAMBA_GID is already used by group $existing_group"
else
    groupadd --gid "$SAMBA_GID_VALUE" "$SAMBA_GROUP"
fi

if getent passwd "$SAMBA_UID_VALUE" >/dev/null 2>&1; then
    existing_user=$(getent passwd "$SAMBA_UID_VALUE" | cut -d: -f1)
    [ "$existing_user" = "$SAMBA_USER_VALUE" ] || fail "SAMBA_UID is already used by user $existing_user"
elif id "$SAMBA_USER_VALUE" >/dev/null 2>&1; then
    fail "SAMBA_USER already exists with a different UID"
else
    useradd \
        --uid "$SAMBA_UID_VALUE" \
        --gid "$SAMBA_GROUP" \
        --no-create-home \
        --home-dir /nonexistent \
        --shell /usr/sbin/nologin \
        "$SAMBA_USER_VALUE"
fi

mkdir -p "$MEDIA_PATH" /run/samba

# Django currently writes the shared volume as root. A shared numeric group plus
# inherited default ACLs lets the non-root Samba account modify both existing and
# future files without running SMB file operations as root.
chgrp -R "$SAMBA_GROUP" "$MEDIA_PATH"
find "$MEDIA_PATH" -type d -exec chmod g+rwx,g+s {} +
find "$MEDIA_PATH" -type f -exec chmod g+rw {} +
setfacl -R -m "g:${SAMBA_GROUP}:rwX,m::rwX" "$MEDIA_PATH"
find "$MEDIA_PATH" -type d -exec setfacl -m "d:g:${SAMBA_GROUP}:rwx,d:m::rwx" {} +

printf '%s\n%s\n' "$SAMBA_PASSWORD_VALUE" "$SAMBA_PASSWORD_VALUE" \
    | smbpasswd -s -a "$SAMBA_USER_VALUE" >/dev/null
smbpasswd -e "$SAMBA_USER_VALUE" >/dev/null

testparm -s /etc/samba/smb.conf >/dev/null
echo "samba-entrypoint: sharing $MEDIA_PATH as //server/files for $SAMBA_USER_VALUE"

unset SAMBA_PASSWORD SAMBA_PASSWORD_FILE SAMBA_PASSWORD_VALUE
exec smbd --foreground --no-process-group --debug-stdout
