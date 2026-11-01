body=$(curl -fsSL --max-time 20 http://127.0.0.1:8001/login) || {
  echo "Kimai does not answer on port 8001" >&2
  exit 1
}
case "$body" in
  *Kimai*) echo "Kimai answers on port 8001" ;;
  *)
    echo "port 8001 answers, but not with the Kimai sign-in page" >&2
    exit 1
    ;;
esac
