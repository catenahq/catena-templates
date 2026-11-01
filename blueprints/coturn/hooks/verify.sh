set -u
fail=""
port_hex() { printf '%04X' "$1"; }
timeout 30 turnutils_uclient -p "$TURN_PORT" -W "$TURN_SECRET" -u catena-verify -n 0 -e 1.1.1.1 127.0.0.1 >/dev/null 2>&1 \
  || fail="$fail allocation-on-${TURN_PORT}-refused"
udp=$(cat /proc/net/udp /proc/net/udp6 2>/dev/null || true)
tcp=$(awk '$4 == "0A" {print $2}' /proc/net/tcp /proc/net/tcp6 2>/dev/null || true)
printf '%s\n' "$udp" | grep -q ":$(port_hex "$TURN_TLS_PORT") " || fail="$fail udp-${TURN_TLS_PORT}-not-listening"
printf '%s\n' "$tcp" | grep -q ":$(port_hex "$TURN_TLS_PORT")\$" || fail="$fail tcp-${TURN_TLS_PORT}-not-listening"
cert=/certs/fullchain.pem
if [ ! -f "$cert" ]; then
  fail="$fail certificate-missing"
else
  end=$(sed -n '1,/END CERTIFICATE/p' "$cert" | sed '/-----/d' | base64 -d 2>/dev/null \
    | tr -c '0-9Z' ' ' | grep -o -E '[0-9]{12}Z' | sed -n 2p)
  if [ -z "$end" ]; then
    fail="$fail certificate-validity-unreadable"
  else
    set -- $(printf %s "$end" | sed 's/^\(..\)\(..\)\(..\)\(..\)\(..\)\(..\)Z$/\1 \2 \3 \4 \5 \6/')
    expires=$(date -u -d "20$1-$2-$3 $4:$5:$6" +%s 2>/dev/null || echo 0)
    [ "$expires" -gt $(($(date -u +%s) + 86400)) ] || fail="$fail certificate-expires-within-24h"
  fi
fi
if [ -n "$fail" ]; then
  echo "the relay check failed:$fail" >&2
  exit 1
fi
echo "the relay allocates on ${TURN_PORT}, listens on ${TURN_TLS_PORT} and its certificate is valid for 24 hours"
