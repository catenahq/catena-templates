set -u
CONFIG=/var/www/html/config
occ() { timeout 60 php /var/www/html/occ "$@"; }
if grep -q "'maintenance' => true" "$CONFIG/config.php" 2>/dev/null; then
  echo "Nextcloud is in maintenance mode, which a backup or an update holds"
  exit 0
fi
if ! occ status --exit-code >/dev/null 2>&1; then
  echo "Nextcloud does not answer: occ status --exit-code failed" >&2
  exit 1
fi
status=$(curl -sS --max-time 20 http://localhost/status.php 2>/dev/null || true)
case "$status" in
  *'"maintenance":true'*)
    echo "Nextcloud is in maintenance mode, which a backup or an update holds"
    exit 0
    ;;
  *'"installed":true'*'"maintenance":false'*) ;;
  *)
    echo "Apache does not answer status.php with an installed instance: '$status'" >&2
    exit 1
    ;;
esac
echo "Nextcloud answers and Apache serves status.php"
