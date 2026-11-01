resp=$(curl -sS --max-time 20 -w '\nHTTP %{http_code}' http://127.0.0.1/s/login) || {
  echo "Mautic does not answer on port 80" >&2
  exit 1
}
code=${resp##*HTTP }
case "$code" in
  200)
    case "$resp" in
      *Mautic*) echo "Mautic answers on port 80"; exit 0 ;;
    esac
    ;;
  301 | 302 | 303 | 307 | 308)
    case "$resp" in
      */installer*) echo "Mautic answers on port 80 and waits for its installer"; exit 0 ;;
    esac
    ;;
esac
echo "port 80 answers $code, but not with a Mautic page" >&2
exit 1
