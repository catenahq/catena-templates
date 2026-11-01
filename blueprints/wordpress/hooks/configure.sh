set -u
WP_CLI=/tmp/wp-cli.phar
SEED=/tmp/catena-seed.php
say() { echo "catena: $*"; }
wp() { php -d memory_limit=512M "$WP_CLI" --path=/var/www/html "$@"; }

if [ ! -f /var/www/html/wp-load.php ]; then
  say "the WordPress files are not there yet; the wiring is left for the next deploy"
  exit 0
fi
if [ ! -f "$WP_CLI" ]; then
  if ! php -r 'copy("https://raw.githubusercontent.com/wp-cli/builds/gh-pages/phar/wp-cli.phar", "/tmp/wp-cli.phar.part");' 2>/dev/null \
      || ! mv /tmp/wp-cli.phar.part "$WP_CLI"; then
    say "wp-cli could not be downloaded; the wiring is left for the next deploy" >&2
    exit 0
  fi
fi

waited=0
until wp core is-installed >/dev/null 2>&1; do
  if [ "$waited" -ge 60 ]; then
    say "the install wizard has not been run; the plugins are installed by the first deploy after it"
    exit 0
  fi
  sleep 2
  waited=$((waited + 2))
done

# Performance Lab ships its modules as separate plugins; the parent
# does not install them.
for slug in kadence-blocks performance-lab auto-sizes dominant-color-images embed-optimizer \
    image-prioritizer optimization-detective speculation-rules webp-uploads \
    fastcgi-cache-purge-and-preload-nginx redis-cache wp-mail-smtp wp-mail-logging \
    complianz-gdpr wp-consent-api seo-by-rank-math fluentform; do
  if ! wp plugin is-installed "$slug" >/dev/null 2>&1; then
    wp plugin install "$slug" --activate >/dev/null 2>&1 \
      || say "$slug could not be installed; the next deploy tries again" >&2
  fi
done

# The constants in WORDPRESS_CONFIG_EXTRA point the plugin at the redis service.
if wp plugin is-active redis-cache >/dev/null 2>&1 && ! wp redis status 2>&1 | grep -q Connected; then
  wp redis enable >/dev/null 2>&1 || say "the Redis object cache could not be enabled" >&2
fi

# Options an administrator also edits are seeded once per site, each
# with a marker option, and are the site's from then on. Keys the site
# already holds win over the values seeded. WP Mail SMTP's relay follows
# Settings on every deploy; its password is not an option: the
# WPMS_SMTP_PASS constant carries it.
cat > "$SEED" <<'PHP'
<?php
if (get_option("catena_npp_seeded") === false) {
  $desired = array(
    "nginx_cache_path" => "/var/cache/nginx/fastcgi",
    "nginx_cache_method" => "unlink",
    "nginx_cache_reject_regex" => "/wp-admin/|/wp-.*\\.php|/feed/|/sitemap.*\\.xml",
    "nginx_cache_auto_purge" => "1",
    "nginx_cache_purge_homepage_on_edit" => "1",
    "nginx_cache_purge_homepage_on_del" => "1",
    "nginx_cache_preload_enable" => "1",
    "nginx_cache_preload_method" => "wget",
    "nginx_cache_preload_sitemap" => "/sitemap_index.xml"
  );
  $current = get_option("nginx_cache_settings");
  if (!is_array($current)) { $current = array(); }
  update_option("nginx_cache_settings", array_merge($desired, $current));
  add_option("catena_npp_seeded", "1", "", "no");
}
$host = (string) getenv("SMTP_RELAY_HOST");
if ($host !== "") {
  $seed = get_option("catena_mail_seeded") === false;
  $sender = (string) getenv("SMTP_RELAY_SENDER");
  $old = get_option("wp_mail_smtp");
  $o = is_array($old) ? $old : array();
  $mail = (isset($o["mail"]) && is_array($o["mail"])) ? $o["mail"] : array();
  $smtp = (isset($o["smtp"]) && is_array($o["smtp"])) ? $o["smtp"] : array();
  if ($seed) {
    // WP Mail SMTP fills the sender with the site's admin email when
    // it is activated, so that value counts as unset.
    $admin = strtolower(trim((string) get_option("admin_email")));
    $from = strtolower(trim((string) (isset($mail["from_email"]) ? $mail["from_email"] : "")));
    if ($admin !== "" && $from === $admin) { unset($mail["from_email"]); }
    $mail = $mail + array(
      "from_email" => $sender, "from_email_force" => true,
      "from_name" => $sender, "from_name_force" => false, "return_path" => false
    );
  }
  $mail["mailer"] = "smtp";
  $smtp = array_merge($smtp, array(
    "host" => $host, "port" => (int) getenv("SMTP_RELAY_PORT"),
    "encryption" => (string) getenv("SMTP_RELAY_TLS"), "autotls" => true, "auth" => true,
    "user" => (string) getenv("SMTP_RELAY_USER")
  ));
  $o["mail"] = $mail;
  $o["smtp"] = $smtp;
  if ($o !== $old) { update_option("wp_mail_smtp", $o); }
  if ($seed) { add_option("catena_mail_seeded", "1", "", "no"); }
}
PHP
status=0
wp eval-file "$SEED" >/dev/null || { echo "catena: the cache and mail settings could not be written" >&2; status=1; }
rm -f "$SEED"
exit "$status"
