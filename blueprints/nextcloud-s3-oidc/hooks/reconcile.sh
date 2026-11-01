set -u
export LC_ALL=C
CONFIG=/var/www/html/config
WAIT=300
say() { echo "catena: $*"; }
fail() { say "$*" >&2; exit 1; }
maintenance() { grep -q "'maintenance' => true" "$CONFIG/config.php" 2>/dev/null; }
if [ -z "${S3_BUCKET:-}" ]; then
  say "this instance keeps its files on disk, so there is no object store to count" >&2
  exit 1
fi
work=$(mktemp -d) || exit 1
trap 'rm -rf "$work"' EXIT
waited=0
until php /var/www/html/occ status --exit-code >/dev/null 2>&1; do
  if maintenance; then
    say "maintenance mode is on; the object store is not counted"
    exit 0
  fi
  if [ "$waited" -ge "$WAIT" ]; then
    say "Nextcloud did not answer within ${WAIT}s" >&2
    exit 1
  fi
  sleep 5
  waited=$((waited + 5))
done
# The URNs the object store holds.
if ! php /var/www/html/occ files:object:list >"$work/listed"; then
  say "files:object:list failed" >&2
  exit 1
fi
tr ' \t|,' '\n\n\n\n' <"$work/listed" >"$work/split" || fail "the bucket listing could not be read"
sed -n 's/^urn:oid:\([0-9][0-9]*\)$/\1/p' "$work/split" >"$work/oids" || fail "the bucket listing could not be read"
sort -u "$work/oids" >"$work/bucket" || fail "the bucket listing could not be sorted"
# The files the database indexes: object-backed storages, no folders, no unknown sizes.
php <<'PHP' >"$work/rows" || { say "the filecache query failed" >&2; exit 1; }
<?php
include "/var/www/html/config/config.php";
$prefix = !empty($CONFIG["dbtableprefix"]) ? $CONFIG["dbtableprefix"] : "oc_";
try {
  $db = new PDO(
    "pgsql:host=".getenv("POSTGRES_HOST").";dbname=".getenv("POSTGRES_DB"),
    getenv("POSTGRES_USER"), getenv("POSTGRES_PASSWORD"),
    [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION]
  );
  $rows = $db->query("SELECT fc.fileid FROM ".$prefix."filecache fc JOIN ".$prefix."storages st ON st.numeric_id = fc.storage WHERE st.id LIKE 'object::%' AND fc.size >= 0 AND fc.mimetype <> (SELECT id FROM ".$prefix."mimetypes WHERE mimetype = 'httpd/unix-directory')");
  while (($id = $rows->fetchColumn()) !== false) {
    echo $id, "\n";
  }
} catch (Throwable $e) {
  fwrite(STDERR, "catena: ".$e->getMessage()."\n");
  exit(2);
}
PHP
sed -n 's/^\([0-9][0-9]*\)$/\1/p' "$work/rows" >"$work/ids" || fail "the filecache rows could not be read"
sort -u "$work/ids" >"$work/database" || fail "the filecache rows could not be sorted"
bucket=$(wc -l <"$work/bucket" | tr -d ' ')
database=$(wc -l <"$work/database" | tr -d ' ')
if [ "$database" -eq 0 ]; then
  say "the filecache lists no object-backed file, so the query does not see the schema it expects" >&2
  exit 1
fi
comm -23 "$work/database" "$work/bucket" >"$work/missing" || fail "the comparison did not complete"
missing=$(wc -l <"$work/missing" | tr -d ' ')
if [ "$missing" -gt 0 ]; then
  say "not in the object store: $(head -n 5 "$work/missing" | sed 's/^/urn:oid:/' | tr '\n' ' ')"
fi
echo "missing=$missing bucket=$bucket database=$database"
