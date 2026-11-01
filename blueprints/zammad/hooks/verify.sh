body=$(curl -fsS --max-time 20 http://127.0.0.1:8080/api/v1/signshow) || {
  echo "Zammad does not answer /api/v1/signshow on port 8080" >&2
  exit 1
}
case "$body" in
  *'"config"'*) echo "Zammad answers /api/v1/signshow through nginx" ;;
  *)
    echo "port 8080 answers, but not with the Zammad sign-in state" >&2
    exit 1
    ;;
esac
