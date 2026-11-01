import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Sets up the Beszel hub through its PocketBase API, from the relay service, every
# time the app is deployed. The folder's .env is this script's environment:
#   ADMIN_EMAIL, HUB_PASSWORD   the first user and the superuser
#   TOKEN                       the universal token the agent connects with
#   HUB_OIDC_ISSUER, HUB_OIDC_SECRET
#                               the hub's sign-in with Catena, when both are set
#   SMTP_RELAY_HOST, SMTP_RELAY_PORT, SMTP_RELAY_USER, SMTP_RELAY_TLS,
#   SMTP_RELAY_SENDER, SMTP_PASSWORD
#                               the outgoing mail chosen in Settings
#   HC_URL, HC_PING_KEY         the Healthchecks the relay pings, when one is installed
#   STATE_KEY                   keys the fingerprint of the mail password
# The superuser keeps password sign-in as the break-glass account: this script signs
# in as it on every run, and Beszel is the tool that diagnoses the sign-in tool.
# The agent-key volume, which the agent mounts read-only, holds the hub's public key
# (key.pub) and the token (token, mode 0600) and nothing else. The relay-state volume,
# which only the relay mounts, holds the ping address (ping-url, mode 0600) and the
# state file: the admin email last signed in with and a keyed fingerprint of the mail
# password last written, neither of which the hub answers back.

BASE = "http://app:8090"
KEY_DIR = "/key"
KEY_FILE = KEY_DIR + "/key.pub"
TOKEN_FILE = KEY_DIR + "/token"
STATE_DIR = "/state"
STATE_FILE = STATE_DIR + "/state.json"
PING_FILE = STATE_DIR + "/ping-url"
WEBHOOK = "generic+http://relay:8099/notify"
HUB_WAIT_S = 120
SYSTEM_WAIT_S = 240
POLL_S = 5
TIMEOUT = 10
PROVIDER = "oidc"
CLIENT_ID = "beszel"
DISPLAY_NAME = "Catena"


def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def req(method, path, *, token=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = token
    request = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as resp:
        raw = resp.read().decode()
    return json.loads(raw) if raw else {}


def quoted(flt):
    return urllib.parse.quote(flt)


def read_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def write_file(path, text, mode):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def wait_for_hub():
    """Wait until the hub answers its first-run question: a deploy ends before the
    hub's own start-up does."""
    deadline = time.monotonic() + HUB_WAIT_S
    while True:
        try:
            return req("GET", "/api/beszel/first-run").get("firstRun")
        except (urllib.error.URLError, OSError) as exc:
            if time.monotonic() >= deadline:
                die(f"the hub does not answer after {HUB_WAIT_S} s: {exc}")
            time.sleep(POLL_S)


def claim_first_run(first, email, password):
    """Make the admin the hub's first user and its superuser while it has no user. A
    hub that has a user is left alone; one claimed by somebody else answers 403,
    which fails the deploy."""
    if first is True:
        req("POST", "/api/beszel/create-user", body={"email": email, "password": password})


def sign_in(identities, password):
    """Authenticate as the superuser under the first identity the hub accepts."""
    for identity in identities:
        try:
            auth = req("POST", "/api/collections/_superusers/auth-with-password",
                       body={"identity": identity, "password": password})
            return auth, identity
        except urllib.error.HTTPError as exc:
            if exc.code != 400:
                raise
    die(f"no superuser for {identities!r} (hub set-up incomplete?)")


def move_superuser(auth, new):
    req("PATCH", f"/api/collections/_superusers/records/{auth['record']['id']}",
        token=auth["token"], body={"email": new})


def rows(token, collection, flt):
    return req("GET", f"/api/collections/{collection}/records?perPage=200&filter=({quoted(flt)})",
               token=token).get("items") or []


def find_users(token, email):
    """The users records holding `email` in any letter case: an address is one
    mailbox however it is capitalised, and the hub compares it exactly, so its unique
    email index lets several coexist."""
    found = rows(token, "users", f"email~'{email}'")
    return [u for u in found if str(u.get("email", "")).lower() == email.lower()]


def merge_user(token, keep_id, drop_id):
    """Give the users record `keep_id` everything the duplicate `drop_id` holds, then
    delete the duplicate. Systems are re-pointed at the kept record. The duplicate's
    settings and alert rules move when the kept record has none of the same, else the
    kept record's win. Its universal token is deleted: the kept record's is the one
    this script reconciles."""
    for system in rows(token, "systems", f"users?='{drop_id}'"):
        owners = [u for u in (system.get("users") or []) if u != drop_id]
        if keep_id not in owners:
            owners.append(keep_id)
        req("PATCH", f"/api/collections/systems/records/{system['id']}", token=token,
            body={"users": owners})

    kept_settings = rows(token, "user_settings", f"user='{keep_id}'")
    for row in rows(token, "user_settings", f"user='{drop_id}'"):
        if kept_settings:
            req("DELETE", f"/api/collections/user_settings/records/{row['id']}", token=token)
        else:
            req("PATCH", f"/api/collections/user_settings/records/{row['id']}", token=token,
                body={"user": keep_id})

    have = {(a.get("system"), a.get("name")) for a in rows(token, "alerts", f"user='{keep_id}'")}
    for row in rows(token, "alerts", f"user='{drop_id}'"):
        key = (row.get("system"), row.get("name"))
        if key in have:
            req("DELETE", f"/api/collections/alerts/records/{row['id']}", token=token)
        else:
            have.add(key)
            req("PATCH", f"/api/collections/alerts/records/{row['id']}", token=token,
                body={"user": keep_id})

    for row in rows(token, "universal_tokens", f"user='{drop_id}'"):
        req("DELETE", f"/api/collections/universal_tokens/records/{row['id']}", token=token)
    req("DELETE", f"/api/collections/users/records/{drop_id}", token=token)


def follow_admin_user(token, new, olds):
    """The admin's users record, moved onto `new` (the lower case address, the form
    Keycloak sends in X-Forwarded-Email) unless it already holds it letter for letter.
    Every record holding `new` or one of `olds` in any letter case is a candidate: a
    Keycloak button sign-in creates a record of its own under the lower case address
    beside a mixed case one. The kept record is the one owning the universal token,
    else the oldest; the others are merged into it and deleted before the email is
    patched, so the unique email index cannot refuse the move. The kept record keeps
    its id, so the universal token, the alert settings and the rules stay attached."""
    found = {}
    for email in [new] + [o for o in olds if o]:
        for user in find_users(token, email):
            found.setdefault(user["id"], user)
    if not found:
        return None
    candidates = list(found.values())
    keep = candidates[0]
    if len(candidates) > 1:
        owner = next((u for u in candidates if rows(token, "universal_tokens", f"user='{u['id']}'")), None)
        keep = owner or min(candidates, key=lambda u: str(u.get("created") or ""))
        for other in candidates:
            if other["id"] != keep["id"]:
                merge_user(token, keep["id"], other["id"])
    if keep.get("email") != new:
        req("PATCH", f"/api/collections/users/records/{keep['id']}", token=token, body={"email": new})
    return keep


def configure_token(token, user_id, universal):
    """The one universal_tokens row of the user (unique on user). With it, an agent
    that connects with TOKEN=<that token> registers its system on its first
    WebSocket connection."""
    found = rows(token, "universal_tokens", f"user='{user_id}'")
    if not found:
        req("POST", "/api/collections/universal_tokens/records", token=token,
            body={"user": user_id, "token": universal})
        return "minted"
    if found[0].get("token") == universal:
        return "ok-exists"
    req("PATCH", f"/api/collections/universal_tokens/records/{found[0]['id']}", token=token,
        body={"token": universal})
    return "updated"


def configure_oidc(token, issuer, secret):
    """Find-or-update the `oidc` provider on the users collection, pointed at the
    issuer. A provider a person configured by hand is kept. The write is read back,
    because PocketBase drops keys it does not recognise instead of refusing them."""
    issuer = issuer.rstrip("/")
    desired = {
        "name": PROVIDER,
        "clientId": CLIENT_ID,
        "clientSecret": secret,
        "authURL": issuer + "/protocol/openid-connect/auth",
        "tokenURL": issuer + "/protocol/openid-connect/token",
        "userInfoURL": issuer + "/protocol/openid-connect/userinfo",
        "displayName": DISPLAY_NAME,
    }
    compared = ("name", "clientId", "authURL", "tokenURL", "userInfoURL", "displayName")
    users = req("GET", "/api/collections/users", token=token)
    oauth2 = dict(users.get("oauth2") or {})
    providers = list(oauth2.get("providers") or [])
    existing = next((p for p in providers if p.get("name") == PROVIDER), None)
    # clientSecret may be masked on read, so it counts only when the hub returns one.
    if (existing and oauth2.get("enabled")
            and all(str(existing.get(k, "")) == str(desired[k]) for k in compared)
            and str(existing.get("clientSecret", "")) in ("", secret)):
        return "ok-exists"
    merged = [p for p in providers if p.get("name") != PROVIDER] + [desired]
    req("PATCH", "/api/collections/users", token=token, body={"oauth2": {"enabled": True, "providers": merged}})
    after = req("GET", "/api/collections/users", token=token).get("oauth2") or {}
    landed = next((p for p in (after.get("providers") or []) if p.get("name") == PROVIDER), None)
    if not landed:
        die(f"wrote the {PROVIDER} provider but it is absent on read-back")
    if not after.get("enabled"):
        die("wrote the oauth2 config but the users collection reports it disabled")
    missing = [k for k in compared if str(landed.get(k, "")) != str(desired[k])]
    if missing:
        die(f"{PROVIDER} provider landed with {missing} not matching what was sent")
    return "updated" if existing else "configured"


def configure_alert_delivery(token, user_id, admin_email):
    """Point the user's notifications at the relay, and keep the admin's address off
    Beszel's own alert mail: a Beszel alert reaches Healthchecks through the relay and
    Healthchecks mails the admin, so Beszel mail as well would send two emails for one
    alert. Every other address a person adds in the hub keeps Beszel's mail, and every
    other webhook is kept."""
    found = rows(token, "user_settings", f"user='{user_id}'")
    current = dict(found[0].get("settings") or {}) if found else {}
    hooks = list(current.get("webhooks") or [])
    emails = list(current.get("emails") or [])
    kept = [e for e in emails if e.strip().lower() != admin_email.strip().lower()]
    if WEBHOOK in hooks and kept == emails:
        return "ok-exists"
    merged = dict(current)
    merged["webhooks"] = hooks if WEBHOOK in hooks else hooks + [WEBHOOK]
    merged["emails"] = kept
    if found:
        req("PATCH", f"/api/collections/user_settings/records/{found[0]['id']}", token=token,
            body={"settings": merged})
        return "updated"
    created = req("POST", "/api/collections/user_settings/records", token=token,
                  body={"user": user_id, "settings": merged})
    # The hub's create hook replaces `emails` with the account's own address, so the
    # new record is written again, which no hook rewrites.
    req("PATCH", f"/api/collections/user_settings/records/{created['id']}", token=token,
        body={"settings": merged})
    return "configured"


def configure_smtp(token, written):
    """Point the hub's mail at the relay chosen in Settings; `written` is the
    fingerprint of the password the last write sent. Returns the outcome and the
    fingerprint of the password the hub now holds."""
    host = os.environ.get("SMTP_RELAY_HOST", "").strip()
    try:
        port = int(os.environ.get("SMTP_RELAY_PORT", "").strip() or "587")
    except ValueError:
        port = 587
    password = os.environ.get("SMTP_PASSWORD", "")
    # tls is implicit TLS from the first byte (ssl); every other mode is STARTTLS.
    desired = {
        "enabled": bool(host),
        "host": host,
        "port": port,
        "username": os.environ.get("SMTP_RELAY_USER", "").strip(),
        "password": password,
        "authMethod": "PLAIN",
        "tls": os.environ.get("SMTP_RELAY_TLS", "").strip().lower() == "ssl",
    }
    compared = ("enabled", "host", "port", "username", "tls")
    current_all = req("GET", "/api/settings", token=token)
    current = dict(current_all.get("smtp") or {})
    if not desired["enabled"]:
        # No mail chosen: the hub's mail goes off with it, so its credential stops being used.
        if current.get("enabled"):
            req("PATCH", "/api/settings", token=token, body={"smtp": {"enabled": False}})
            return "disabled", ""
        return "no-mail", ""
    meta = dict(current_all.get("meta") or {})
    sender = os.environ.get("SMTP_RELAY_SENDER", "").strip()
    fingerprint = hmac.new(os.environ["STATE_KEY"].encode("utf-8"), password.encode("utf-8"),
                           hashlib.sha256).hexdigest()
    same = all(str(current.get(k, "")) == str(desired[k]) for k in compared)
    meta_same = str(meta.get("senderAddress", "")) == sender and str(meta.get("senderName", "")) == DISPLAY_NAME
    # PocketBase blanks the password in every settings read, so the fingerprint of the
    # last write stands in for it.
    if same and meta_same and written == fingerprint:
        return "ok-exists", fingerprint
    req("PATCH", "/api/settings", token=token,
        body={"smtp": desired, "meta": {"senderName": DISPLAY_NAME, "senderAddress": sender}})
    after = dict(req("GET", "/api/settings", token=token).get("smtp") or {})
    drifted = [k for k in compared if str(after.get(k, "")) != str(desired[k])]
    if drifted:
        die(f"wrote the mail settings but {drifted} did not land")
    return ("updated" if str(current.get("host") or "").strip() else "configured"), fingerprint


def write_hub_key(token):
    """The hub's public key, which the agent reads from key.pub. Written whole by
    rename, and only when it differs."""
    key = str(req("GET", "/api/beszel/info", token=token).get("key") or "").strip()
    if not key:
        die("the hub answered no public key")
    text = key + "\n"
    try:
        with open(KEY_FILE, encoding="utf-8") as fh:
            if fh.read() == text:
                return "ok-exists"
    except OSError:
        pass
    write_file(KEY_FILE, text, 0o644)
    return "written"


def write_agent_token(universal):
    """The universal token, which the agent reads from token. Written whole by rename."""
    try:
        with open(TOKEN_FILE, encoding="utf-8") as fh:
            if fh.read() == universal + "\n":
                return "ok-exists"
    except OSError:
        pass
    write_file(TOKEN_FILE, universal + "\n", 0o600)
    return "written"


def write_ping_address():
    """The Healthchecks ping address up to the check name, which the relay reads for each
    alert; removed while no Healthchecks is installed."""
    base = os.environ.get("HC_URL", "").strip().rstrip("/")
    key = os.environ.get("HC_PING_KEY", "").strip()
    if not base or not key:
        try:
            os.remove(PING_FILE)
        except OSError:
            return "ok-exists"
        return "removed"
    text = f"{base}/ping/{key}\n"
    try:
        with open(PING_FILE, encoding="utf-8") as fh:
            if fh.read() == text:
                return "ok-exists"
    except OSError:
        pass
    write_file(PING_FILE, text, 0o600)
    return "written"


# The alert rules every system gets: the status rule fires when the agent stops
# reporting, the others when a metric stays past its value for `min` minutes. A rule is
# created and never updated, so a threshold a person changed stays.
RULES = (
    {"name": "Status", "value": 0, "min": 5},
    {"name": "CPU", "value": 90, "min": 10},
    {"name": "Memory", "value": 90, "min": 10},
    {"name": "Disk", "value": 85, "min": 10},
)


def configure_alert_rules(token, user_id):
    """Create the default rules for every system, once the agent's system is listed."""
    deadline = time.monotonic() + SYSTEM_WAIT_S
    while True:
        systems = req("GET", "/api/collections/systems/records?perPage=200", token=token).get("items") or []
        if systems:
            break
        if time.monotonic() >= deadline:
            die(f"the agent's system is not listed after {SYSTEM_WAIT_S} s")
        time.sleep(POLL_S)
    created = 0
    for system in systems:
        have = {r.get("name") for r in rows(token, "alerts", f"user='{user_id}' && system='{system['id']}'")}
        for rule in RULES:
            if rule["name"] in have:
                continue
            req("POST", "/api/collections/alerts/records", token=token,
                body={"user": user_id, "system": system["id"], "name": rule["name"],
                      "value": rule["value"], "min": rule["min"]})
            created += 1
    return "ok-exists" if created == 0 else f"created {created}"


def main():
    email = os.environ["ADMIN_EMAIL"]
    password = os.environ["HUB_PASSWORD"]
    universal = os.environ["TOKEN"]
    issuer = os.environ.get("HUB_OIDC_ISSUER", "").strip()
    oidc_secret = os.environ.get("HUB_OIDC_SECRET", "")
    if not email:
        die("ADMIN_EMAIL is empty")

    # The superuser keeps the admin email's letter case, which the hub compares exactly
    # on a password sign-in; the users record holds the lower case address. After an
    # admin email change the superuser signs in with the address recorded last time and
    # both records move to the new one.
    claim_first_run(wait_for_hub(), email, password)
    state = read_state()
    recorded = str(state.get("admin_email") or "")
    auth, signed_in_as = sign_in([email] + ([recorded] if recorded and recorded != email else []), password)
    if signed_in_as != email:
        move_superuser(auth, email)
        auth, _ = sign_in([email], password)
    token = auth.get("token")
    if not token:
        die("superuser auth returned no token")
    user = follow_admin_user(token, email.lower(), [email, recorded, signed_in_as])
    if not user:
        die(f"no users record for {email!r} (hub set-up incomplete?)")
    state["admin_email"] = email
    write_file(STATE_FILE, json.dumps(state), 0o600)

    outcome = {"universal-token": configure_token(token, user["id"], universal)}
    outcome["oidc"] = configure_oidc(token, issuer, oidc_secret) if issuer and oidc_secret else "not-set"
    outcome["alert-delivery"] = configure_alert_delivery(token, user["id"], email)
    outcome["smtp"], state["smtp_password_sha256"] = configure_smtp(token, str(state.get("smtp_password_sha256") or ""))
    write_file(STATE_FILE, json.dumps(state), 0o600)
    outcome["public-key"] = write_hub_key(token)
    outcome["agent-token"] = write_agent_token(universal)
    outcome["ping-address"] = write_ping_address()
    outcome["alert-rules"] = configure_alert_rules(token, user["id"])
    print("; ".join(f"{k}: {v}" for k, v in outcome.items()))


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as exc:
        die(f"HTTP {exc.code} from the hub API: {exc.read().decode(errors='replace')[:300]}")
    except urllib.error.URLError as exc:
        die(f"the hub API is unreachable: {exc}")
