body=$(wget -q -T 20 -O - http://portainer:9000/) || {
  echo "Portainer does not answer on port 9000" >&2
  exit 1
}
case "$body" in
  *Portainer*) echo "Portainer answers its main page" ;;
  *)
    echo "port 9000 answers, but not with the Portainer page" >&2
    exit 1
    ;;
esac
