body=$(wget -q -T 20 -O - http://app:8090/api/health) || {
  echo "the Beszel hub does not answer on port 8090" >&2
  exit 1
}
case "$body" in
  *healthy*) echo "the Beszel hub answers its health endpoint" ;;
  *)
    echo "port 8090 answers, but not with the hub's health message" >&2
    exit 1
    ;;
esac
