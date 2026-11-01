set -u
CONFIG=/var/www/html/config
USER_OIDC_VERSION=8.11.0
WAIT=900
failed=0
occ() { php /var/www/html/occ "$@"; }
say() { echo "catena: $*"; }
maintenance() { grep -q "'maintenance' => true" "$CONFIG/config.php" 2>/dev/null; }
installed() { grep -q "'installed' => true" "$CONFIG/config.php" 2>/dev/null; }
step() {
  what=$1
  shift
  "$@" >/dev/null || { echo "catena: $what failed" >&2; failed=1; }
}
# 0 when the database holds no Nextcloud table, 1 when it does, 2 when it does not answer.
database_empty() {
  php <<'PHP'
<?php
include "/var/www/html/config/config.php";
$prefix = !empty($CONFIG["dbtableprefix"]) ? $CONFIG["dbtableprefix"] : "oc_";
try {
  $db = new PDO(
    "pgsql:host=".getenv("POSTGRES_HOST").";dbname=".getenv("POSTGRES_DB"),
    getenv("POSTGRES_USER"), getenv("POSTGRES_PASSWORD"),
    [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION]
  );
  $found = $db->query("SELECT count(*) FROM information_schema.tables WHERE table_name = " . $db->quote($prefix . "appconfig"))->fetchColumn();
  exit($found > 0 ? 1 : 0);
} catch (Throwable $e) {
  exit(2);
}
PHP
}
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
app_present() { occ app:getpath "$1" >/dev/null 2>&1; }
app_enabled() {
  occ app:list --output=json 2>/dev/null | php -r '$d = json_decode(stream_get_contents(STDIN), true); exit(isset($d["enabled"][$argv[1]]) ? 0 : 1);' "$1"
}
# An app that is not on disk is installed from the app store, which can
# be down: the next deploy tries again. One that is on disk stays as an
# administrator left it. Returns 0 when the app is enabled.
ensure_app() {
  if ! app_present "$1"; then
    n=0
    until occ app:install "$1" >/dev/null 2>&1 || app_present "$1"; do
      n=$((n + 1))
      if [ "$n" -ge 3 ]; then
        say "$1 could not be installed from the app store; the next deploy tries again" >&2
        return 1
      fi
      sleep 10
    done
  fi
  app_enabled "$1"
}
wire_antivirus() {
  occ config:app:set files_antivirus av_mode --value daemon &&
  occ config:app:set files_antivirus av_host --value "$ANTIVIRUS_HOST" &&
  occ config:app:set files_antivirus av_port --value "$ANTIVIRUS_PORT" &&
  occ config:app:set files_antivirus av_stream_max_length --value 104857600 &&
  occ config:app:set files_antivirus av_max_file_size --value 104857600 &&
  occ config:app:set files_antivirus av_infected_action --value only_log &&
  occ config:app:set files_antivirus av_block_unreachable --type boolean --value true
}
webmail_link() {
  occ config:system:set external_sites 0 id --value 1 --type integer &&
  occ config:system:set external_sites 0 name --value Webmail &&
  occ config:system:set external_sites 0 url --value "$1" &&
  occ config:system:set external_sites 0 icon --value external.svg &&
  occ config:system:set external_sites 0 type --value link &&
  occ config:system:set external_sites 0 lang --value "" &&
  occ config:system:set external_sites 0 device --value "" &&
  occ config:system:set external_sites 0 redirect --value true --type boolean
}

# The Talk entries of the call relay: host, ports and a digest of the
# secret, recorded once written so a changed relay replaces them and a
# removed relay takes them away.
turn_record() {
  printf '%s %s %s %s' "$TURN_HOSTNAME" "$TURN_PORT" "$TURN_TLS_PORT" "$(printf %s "$TURN_SECRET" | sha256sum | cut -c1-16)"
}
unwire_turn() {
  set -- $(occ config:app:get catena turn_wired 2>/dev/null || true)
  [ "$#" -eq 4 ] || return 0
  occ talk:turn:delete turn "$1:$2" udp &&
  occ talk:turn:delete turns "$1:$3" tcp &&
  occ talk:stun:delete "$1:$2" &&
  occ config:app:delete catena turn_wired
}
wire_turn() {
  [ "$(occ config:app:get catena turn_wired 2>/dev/null || true)" != "$(turn_record)" ] || return 0
  unwire_turn &&
  occ talk:turn:delete turn "$TURN_HOSTNAME:$TURN_PORT" udp &&
  occ talk:turn:delete turns "$TURN_HOSTNAME:$TURN_TLS_PORT" tcp &&
  occ talk:stun:delete "$TURN_HOSTNAME:$TURN_PORT" &&
  occ talk:turn:add turn "$TURN_HOSTNAME:$TURN_PORT" udp --secret="$TURN_SECRET" &&
  occ talk:turn:add turns "$TURN_HOSTNAME:$TURN_TLS_PORT" tcp --secret="$TURN_SECRET" &&
  occ talk:stun:add "$TURN_HOSTNAME:$TURN_PORT" &&
  occ config:app:set catena turn_wired --value "$(turn_record)"
}
# The signaling server entry, recorded the same way: Talk adds a
# duplicate entry for each add.
signaling_record() {
  printf '%s %s' "https://$SIGNALING_HOSTNAME" "$(printf %s "$SIGNALING_SECRET" | sha256sum | cut -c1-16)"
}
unwire_signaling() {
  set -- $(occ config:app:get catena signaling_wired 2>/dev/null || true)
  [ "$#" -eq 2 ] || return 0
  occ talk:signaling:delete "$1" &&
  { occ config:app:delete spreed external_signaling_only || true; } &&
  occ config:app:delete catena signaling_wired
}
wire_signaling() {
  [ "$(occ config:app:get catena signaling_wired 2>/dev/null || true)" != "$(signaling_record)" ] || return 0
  unwire_signaling &&
  occ talk:signaling:add "https://$SIGNALING_HOSTNAME" "$SIGNALING_SECRET" &&
  occ config:app:set spreed external_signaling_only --value=yes &&
  occ config:app:set catena signaling_wired --value "$(signaling_record)"
}

# The app each office editor runs under in Nextcloud, and the app-config
# keys its removal can leave behind.
office_app() { if [ "$1" = collabora ]; then echo richdocuments; else echo onlyoffice; fi; }
office_keys() {
  if [ "$1" = collabora ]; then
    echo wopi_url public_wopi_url wopi_callback_url external_apps disable_certificate_verification secret edit_groups read_only_groups
  else
    echo DocumentServerUrl DocumentServerInternalUrl StorageUrl jwt_secret editingMode defFormats editFormats sameTab customizationGoback versionHistory
  fi
}
office_alive() {
  if [ "$OFFICE_EDITOR" = collabora ]; then
    curl -fsS --max-time 5 "$OFFICE_URL/hosting/discovery" 2>/dev/null | grep -q '<wopi-discovery'
  else
    curl -fsS --max-time 5 "$OFFICE_URL/healthcheck" 2>/dev/null | grep -qx true
  fi
}
drop_office() {
  app=$(office_app "$1")
  app_present "$app" || return 0
  occ app:remove "$app" || return 1
  for k in $(office_keys "$1"); do
    occ config:app:delete "$app" "$k" || true
  done
}
wire_office() {
  if [ "$OFFICE_EDITOR" = onlyoffice ] && [ -z "${OFFICE_JWT_SECRET:-}" ]; then
    echo "catena: the OnlyOffice editor gave no JWT secret" >&2
    return 1
  fi
  ensure_app "$(office_app "$OFFICE_EDITOR")" || return 1
  if [ "$OFFICE_EDITOR" = collabora ]; then
    occ config:app:set richdocuments wopi_url --value="$OFFICE_URL" &&
    occ config:app:set richdocuments public_wopi_url --value="$OFFICE_URL" &&
    occ config:app:set richdocuments disable_certificate_verification --value=no &&
    { occ richdocuments:activate-config || true; }
  else
    occ config:app:set onlyoffice DocumentServerUrl --value="$OFFICE_URL/" &&
    occ config:app:set onlyoffice DocumentServerInternalUrl --value="$OFFICE_URL/" &&
    occ config:app:set onlyoffice StorageUrl --value="" &&
    occ config:app:set onlyoffice jwt_secret --value="$OFFICE_JWT_SECRET"
  fi
}

# Nextcloud's outgoing mail is the server's mail relay. The settings
# written are recorded with a digest of the password: a changed relay
# replaces them, an unchanged one leaves a person's own mail settings
# alone, and a removed one takes them away.
mail_record() {
  printf '%s|%s|%s|%s|%s|%s' "${SMTP_RELAY_HOST:-}" "${SMTP_RELAY_PORT:-}" "${SMTP_RELAY_TLS:-}" "${SMTP_RELAY_USER:-}" "${SMTP_RELAY_SENDER:-}" "$(printf %s "${SMTP_RELAY_PASSWORD:-}" | sha256sum | cut -c1-16)"
}
mail_login() {
  if [ -n "${SMTP_RELAY_USER:-}" ]; then
    occ config:system:set mail_smtpauth --value true --type boolean &&
    occ config:system:set mail_smtpname --value "$SMTP_RELAY_USER" &&
    occ config:system:set mail_smtppassword --value "${SMTP_RELAY_PASSWORD:-}"
  else
    occ config:system:set mail_smtpauth --value false --type boolean &&
    occ config:system:delete mail_smtpname &&
    occ config:system:delete mail_smtppassword
  fi
}
mail_sender() {
  case "${SMTP_RELAY_SENDER:-}" in
    ?*@?*)
      occ config:system:set mail_from_address --value "${SMTP_RELAY_SENDER%@*}" &&
      occ config:system:set mail_domain --value "${SMTP_RELAY_SENDER##*@}"
      ;;
    *)
      occ config:system:delete mail_from_address &&
      occ config:system:delete mail_domain
      ;;
  esac
}
wire_mail() {
  [ "$(occ config:app:get catena mail_wired 2>/dev/null || true)" != "$(mail_record)" ] || return 0
  secure=""
  [ "${SMTP_RELAY_TLS:-}" != ssl ] || secure=ssl
  occ config:system:set mail_smtpmode --value smtp &&
  occ config:system:set mail_smtphost --value "$SMTP_RELAY_HOST" &&
  occ config:system:set mail_smtpport --value "${SMTP_RELAY_PORT:-587}" --type integer &&
  occ config:system:set mail_smtpsecure --value "$secure" &&
  mail_login &&
  mail_sender &&
  occ config:app:set catena mail_wired --value "$(mail_record)"
}
unwire_mail() {
  [ -n "$(occ config:app:get catena mail_wired 2>/dev/null || true)" ] || return 0
  for key in mail_smtpmode mail_smtphost mail_smtpport mail_smtpsecure mail_smtpauth mail_smtpname mail_smtppassword mail_from_address mail_domain; do
    occ config:system:delete "$key" || return 1
  done
  occ config:app:delete catena mail_wired
}

# The object store reads the OBJECTSTORE_S3_* variables of the container
# through the image's s3.config.php, which a restored config volume may lack.
# The file is put back only for an instance whose database holds the object
# store of the bucket. On any other installed instance, which keeps its files
# on disk, the file is removed: with a bucket in the environment it would
# switch the instance to an empty object store. The image copies it only into
# an empty config folder, so an image update does not bring it back.
if [ -n "${S3_BUCKET:-}" ] && [ -f "$CONFIG/config.php" ]; then
  state=0
  object_store_state || state=$?
  case "$state" in
    0)
      if [ ! -f "$CONFIG/s3.config.php" ] && [ -f /usr/src/nextcloud/config/s3.config.php ]; then
        cp /usr/src/nextcloud/config/s3.config.php "$CONFIG/s3.config.php"
        say "the object store file was put back from the image"
      fi
      ;;
    1)
      if installed && ! maintenance; then
        say "this instance keeps its files on disk; the bucket setting is ignored"
        if [ -f "$CONFIG/s3.config.php" ]; then
          rm -f "$CONFIG/s3.config.php" && say "the object store file was removed"
        fi
      fi
      ;;
  esac
fi

waited=0
until occ status --exit-code >/dev/null 2>&1; do
  if maintenance; then
    say "maintenance mode is on; the wiring is left for the next deploy"
    exit 0
  fi
  if installed && database_empty; then
    say "the database holds no Nextcloud table yet; the restored database carries the wiring"
    exit 0
  fi
  if [ "$waited" -ge "$WAIT" ]; then
    say "Nextcloud did not answer within ${WAIT}s; the wiring is left for the next deploy" >&2
    exit 0
  fi
  sleep 5
  waited=$((waited + 5))
done

if [ -n "${OIDC_CLIENT_ID:-}" ] && [ -n "${OIDC_CLIENT_SECRET:-}" ] && [ -n "${OIDC_ISSUER_URL:-}" ]; then
  if ensure_app user_oidc; then
    have=$(occ config:app:get user_oidc installed_version 2>/dev/null || true)
    if [ "${have%%.*}" != "${USER_OIDC_VERSION%%.*}" ] \
        || ! printf '%s\n%s\n' "$USER_OIDC_VERSION" "$have" | sort -V -C; then
      echo "catena: user_oidc $have is installed; the provider command is written for user_oidc $USER_OIDC_VERSION or a newer release of the same major version" >&2
      failed=1
    else
      discovery="${OIDC_ISSUER_URL%/}/.well-known/openid-configuration"
      shown=$(occ user_oidc:provider keycloak --output=json 2>/dev/null) || shown=""
      if [ -n "$shown" ]; then
        step "the sign-in provider update" occ user_oidc:provider keycloak --no-interaction \
          --clientid="$OIDC_CLIENT_ID" --clientsecret="$OIDC_CLIENT_SECRET" --discoveryuri="$discovery"
      else
        step "the sign-in provider" occ user_oidc:provider keycloak --no-interaction \
          --clientid="$OIDC_CLIENT_ID" --clientsecret="$OIDC_CLIENT_SECRET" --discoveryuri="$discovery" \
          --scope="openid email profile groups" --mapping-uid=preferred_username \
          --mapping-display-name=name --mapping-email=email --mapping-groups=groups \
          --group-provisioning=1
      fi
    fi
  fi
else
  say "the sign-in entry values are not in the environment yet; Keycloak is wired when they are"
fi

ensure_app mail || true

if [ -n "${ANTIVIRUS_HOST:-}" ] && [ -n "${ANTIVIRUS_PORT:-}" ] && ensure_app files_antivirus \
    && [ -z "$(occ config:app:get catena antivirus_wired 2>/dev/null)" ]; then
  if wire_antivirus >/dev/null; then
    occ config:app:set catena antivirus_wired --value 1 >/dev/null
  else
    echo "catena: the antivirus settings failed" >&2
    failed=1
  fi
fi

if ensure_app spreed; then
  if [ -n "${TURN_HOSTNAME:-}" ] && [ -n "${TURN_SECRET:-}" ]; then
    step "the Talk relay entries" wire_turn
    if [ -n "${SIGNALING_HOSTNAME:-}" ] && [ -n "${SIGNALING_SECRET:-}" ] \
        && curl -fsS --max-time 3 http://signaling:8081/api/v1/welcome >/dev/null 2>&1; then
      step "the Talk signaling entry" wire_signaling
    fi
  else
    step "the Talk relay entries removal" unwire_turn
    step "the Talk signaling entry removal" unwire_signaling
  fi
fi

if [ -n "${OFFICE_EDITOR:-}" ] && [ -n "${OFFICE_URL:-}" ]; then
  if [ "$OFFICE_EDITOR" = collabora ]; then other=onlyoffice; else other=collabora; fi
  waited=0
  until office_alive || [ "$waited" -ge 300 ]; do
    sleep 5
    waited=$((waited + 5))
  done
  if office_alive; then
    step "the $other editor removal" drop_office "$other"
    step "the $OFFICE_EDITOR editor" wire_office
  else
    echo "catena: the $OFFICE_EDITOR editor at $OFFICE_URL does not answer; the next deploy wires it" >&2
    failed=1
  fi
fi

if [ -n "${SMTP_RELAY_HOST:-}" ]; then
  step "the outgoing mail settings" wire_mail
else
  step "the outgoing mail settings removal" unwire_mail
fi

if [ -n "${WEBMAIL_HOSTNAME:-}" ] && getent hosts catena-mailserver_roundcube >/dev/null 2>&1; then
  if ensure_app external; then
    current=$(occ config:system:get external_sites 0 url 2>/dev/null || true)
    if [ "$current" != "https://$WEBMAIL_HOSTNAME" ]; then
      step "the Webmail link" webmail_link "https://$WEBMAIL_HOSTNAME"
    fi
  fi
fi

exit "$failed"
