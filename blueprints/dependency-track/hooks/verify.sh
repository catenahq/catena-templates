body=$(wget -q -T 20 -O - http://127.0.0.1:8080/api/version) || {
  echo "Dependency-Track does not answer on port 8080" >&2
  exit 1
}
case "$body" in
  *'"version"'*) echo "Dependency-Track answers /api/version" ;;
  *)
    echo "port 8080 answers, but not with the Dependency-Track version" >&2
    exit 1
    ;;
esac
