body=$(wget -q -T 20 -O - http://app:8080/health) || {
  echo "Gatus does not answer on port 8080" >&2
  exit 1
}
case "$body" in
  *UP*) echo "Gatus answers its health endpoint" ;;
  *)
    echo "port 8080 answers, but not with Gatus's health status" >&2
    exit 1
    ;;
esac
