end=$(( $(date +%s) + 150 ))
while :; do
  body=$(wget -q -T 10 -O - http://127.0.0.1/health 2>&1)
  rc=$?
  [ "$rc" != 0 ] || break
  case "$body" in
    *"Connection refused"*|*"error: HTTP/1.1 502"*|*"error: HTTP/1.1 503"*)
      [ "$(date +%s)" -lt "$end" ] || break
      sleep 4
      ;;
    *) break ;;
  esac
done
if [ "$rc" != 0 ]; then
  echo "Invoice Ninja does not answer /health through nginx: $body" >&2
  exit 1
fi
case "$body" in
  *"API is healthy"*) echo "Invoice Ninja answers /health through nginx" ;;
  *)
    echo "/health answers, but not with the Invoice Ninja health reply" >&2
    exit 1
    ;;
esac
