set -u
CONFIG=/var/www/html/config
occ() { timeout 60 php /var/www/html/occ "$@"; }
# 0 when the storages table holds the object store of the configured bucket
# (named object::store:amazon::<bucket>, or the md5 of that name when it is
# longer than 64 characters), 1 when it holds none, which is an instance that
# keeps its files on disk, 2 when the database does not answer or has no
# storages table.
object_store_state() {
  id="object::store:amazon::${S3_BUCKET}"
  if [ "${#id}" -gt 64 ]; then
    id=$(printf '%s' "$id" | md5sum | cut -d' ' -f1)
  fi
  CATENA_STORE_ID="$id" php <<'PHP'
<?php
include "/var/www/html/config/config.php";
$prefix = !empty($CONFIG["dbtableprefix"]) ? $CONFIG["dbtableprefix"] : "oc_";
try {
  $db = new PDO(
    "pgsql:host=".getenv("POSTGRES_HOST").";dbname=".getenv("POSTGRES_DB"),
    getenv("POSTGRES_USER"), getenv("POSTGRES_PASSWORD"),
    [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION]
  );
  $found = $db->query("SELECT count(*) FROM information_schema.tables WHERE table_name = " . $db->quote($prefix . "storages"))->fetchColumn();
  if ($found == 0) {
    exit(2);
  }
  $held = $db->prepare("SELECT count(*) FROM " . $prefix . "storages WHERE id = ?");
  $held->execute([getenv("CATENA_STORE_ID")]);
  exit($held->fetchColumn() > 0 ? 0 : 1);
} catch (Throwable $e) {
  exit(2);
}
PHP
}
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
if [ -n "${S3_BUCKET:-}" ]; then
  state=0
  object_store_state || state=$?
  if [ "$state" -eq 1 ]; then
    echo "this instance keeps its files on disk; the bucket setting is ignored" >&2
    exit 1
  fi
  class=$(occ config:system:get objectstore class 2>/dev/null || true)
  case "$class" in
    *ObjectStore*S3) ;;
    *)
      echo "the object store is not configured: its class reads '$class'" >&2
      exit 1
      ;;
  esac
  bucket=$(occ config:system:get objectstore arguments bucket 2>/dev/null || true)
  if [ "$bucket" != "$S3_BUCKET" ]; then
    echo "Nextcloud uses the bucket '$bucket' while the environment names '$S3_BUCKET'" >&2
    exit 1
  fi
  scheme=https
  case "${S3_SSL:-true}" in
    false|False|FALSE|0) scheme=http ;;
  esac
  if ! curl -sS -o /dev/null --max-time 20 "$scheme://$S3_HOST:${S3_PORT:-443}/"; then
    echo "the object store at $S3_HOST:${S3_PORT:-443} does not answer" >&2
    exit 1
  fi
  echo "Nextcloud answers, Apache serves status.php, Nextcloud uses the bucket $bucket, and the object store at $S3_HOST answers"
else
  echo "Nextcloud answers and Apache serves status.php"
fi
