set -u
cert=/certs/fullchain.pem
mark=/tmp/docker-mailserver/.catena-cert.sha256
[ -f "$cert" ] || exit 0
now=$(sha256sum "$cert" | cut -d' ' -f1)
[ "$now" != "$(cat "$mark" 2>/dev/null)" ] || exit 0
postfix reload >/dev/null 2>&1 || true
doveadm reload >/dev/null 2>&1 || true
echo "$now" > "$mark"
