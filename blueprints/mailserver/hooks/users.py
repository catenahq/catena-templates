import json
import os
import re
import secrets
import subprocess
import sys
import time

STATE = "/tmp/docker-mailserver/.catena-mailboxes.json"
PROTECTED = {"postmaster"}
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def setup(*args):
    return subprocess.run(["setup", *args], capture_output=True, text=True)


def load():
    try:
        with open(STATE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    return {str(a).strip().lower() for a in data if str(a).strip()}


def save(addresses):
    tmp = STATE + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as fh:
        json.dump(sorted(addresses), fh)
    os.replace(tmp, STATE)


def wait_for_users(addresses):
    # The image's watcher copies the source accounts into Dovecot's userdb.
    pending = set(addresses)
    deadline = time.monotonic() + 120
    while pending:
        for address in sorted(pending):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print("Dovecot mailboxes not ready: " + ", ".join(sorted(pending)), file=sys.stderr)
                return False
            try:
                result = subprocess.run(["doveadm", "user", address], capture_output=True,
                                        timeout=min(10, remaining))
            except subprocess.TimeoutExpired:
                continue
            if result.returncode == 0:
                pending.remove(address)
        if pending:
            time.sleep(min(2, max(0, deadline - time.monotonic())))
    return True


domain = os.environ.get("MAIL_DOMAIN", "").strip().lower()
if not domain:
    print("MAIL_DOMAIN is not set; no mailbox is changed", file=sys.stderr)
    sys.exit(1)
members = json.load(sys.stdin)
listed = {m["email"].strip().lower() for m in members if (m.get("email") or "").strip()}
if not listed:
    print("no member has an email address; no mailbox is changed")
    sys.exit(0)
desired = {a for a in listed if a.rpartition("@")[0] and a.rpartition("@")[2] == domain}
listing = setup("email", "list")
if listing.returncode != 0:
    print("the mailboxes could not be listed: " + listing.stderr.strip(), file=sys.stderr)
    sys.exit(1)
current = {m.group(0).lower() for line in listing.stdout.splitlines() for m in [EMAIL.search(line)] if m}
created = load()
if created is None:
    created = listed & current
    save(created)
    print("no record of the mailboxes made here: %d listed and present are taken as made here, none is removed" % len(created))
    deletable = set()
else:
    deletable = set(created)
added = removed = 0
failed = False
for address in sorted(desired - current):
    if setup("email", "add", address, secrets.token_urlsafe(24)).returncode != 0:
        print("adding " + address + " failed", file=sys.stderr)
        failed = True
        continue
    created.add(address)
    added += 1
for address in sorted(current - desired):
    if address not in deletable or address.partition("@")[0] in PROTECTED:
        continue
    if setup("email", "del", "-y", address).returncode != 0:
        print("removing " + address + " failed", file=sys.stderr)
        failed = True
        continue
    created.discard(address)
    removed += 1
if added or removed:
    save(created)
if not failed and not wait_for_users(desired):
    failed = True
print("mailboxes: %d added, %d removed" % (added, removed))
sys.exit(1 if failed else 0)
