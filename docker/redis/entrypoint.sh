#!/bin/sh
set -eu

case "${REDIS_USERNAME:-}" in
  ""|*[!a-zA-Z0-9_-]*)
    echo "REDIS_USERNAME must contain only letters, digits, '_' or '-'" >&2
    exit 1
    ;;
esac

if [ -z "${REDIS_PASSWORD:-}" ]; then
  echo "REDIS_PASSWORD is required" >&2
  exit 1
fi

password_hash="$(printf '%s' "$REDIS_PASSWORD" | sha256sum | awk '{print $1}')"
acl_tmp="/data/users.acl.tmp"
printf 'user default off\nuser %s on #%s ~* +@all\n' "$REDIS_USERNAME" "$password_hash" > "$acl_tmp"
mv "$acl_tmp" /data/users.acl
chmod 600 /data/users.acl

exec redis-server --appendonly yes --aclfile /data/users.acl
