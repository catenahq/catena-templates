end=$(( $(date +%s) + 150 ))
while :; do
  out=$(curl -s --max-time 10 -w '%{http_code}' http://127.0.0.1:3000/healthz)
  rc=$?
  code=${out#${out%???}}
  body=${out%???}
  case "$rc:$code" in
    7:*|*:502|*:503) [ "$(date +%s)" -lt "$end" ] || break; sleep 4 ;;
    *) break ;;
  esac
done
if [ "$rc" != 0 ] || [ "$code" != 200 ]; then
  echo "Twenty does not answer /healthz on port 3000 (curl $rc, status $code)" >&2
  exit 1
fi
case "$body" in
  *'"status":"ok"'*) echo "Twenty answers /healthz" ;;
  *)
    echo "/healthz answers, but not ok: $body" >&2
    exit 1
    ;;
esac
