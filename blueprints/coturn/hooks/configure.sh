set -u
cert=/certs/fullchain.pem
mark=/tmp/.catena-cert.sha256
[ -f "$cert" ] || exit 0
now=$(sha256sum "$cert" | cut -d' ' -f1)
[ "$now" != "$(cat "$mark" 2>/dev/null)" ] || exit 0
kill -USR2 1 || exit 1
echo "$now" > "$mark"
