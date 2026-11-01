set -eu
export CATENA_MEMBERS="$(cat)"
cd /home/frappe/frappe-bench/sites
exec ../env/bin/python - <<'PY'
import json
import os
import sys

import frappe

LEVEL = "customer"
ROLE = "Customer"
KEY = "catena_access_customer"

wanted = {}
for member in json.loads(os.environ.get("CATENA_MEMBERS") or "[]"):
    email = (member.get("email") or "").strip().lower()
    if email and member.get("enabled", True) and LEVEL in (member.get("access") or []):
        wanted[email] = member

try:
    frappe.init(os.environ["SITE_NAME"])
    frappe.connect()
except Exception as error:
    sys.exit("catena access: the site does not answer: " + str(error))


def roles_of(user):
    return {row.role for row in user.roles}


def grant(email, member, state):
    if not frappe.db.exists("User", email):
        user = frappe.new_doc("User")
        user.update({"name": email, "email": email, "enabled": 1,
                     "first_name": member.get("first_name") or email.split("@")[0],
                     "last_name": member.get("last_name") or "",
                     "user_type": "Website User", "send_welcome_email": 0})
        user.insert()
        user.add_roles(ROLE)
        return "made"
    user = frappe.get_doc("User", email)
    if ROLE in roles_of(user):
        return state if state in ("made", "added") else "had"
    if state == "closed":
        user.enabled = 1
    user.add_roles(ROLE)
    return "made" if state in ("made", "closed") else "added"


def revoke(email, state):
    if state == "closed":
        return state
    if state == "had" or not frappe.db.exists("User", email):
        return None
    user = frappe.get_doc("User", email)
    user.remove_roles(ROLE)
    if state == "made" and not user.roles:
        user.enabled = 0
        user.save()
        return "closed"
    return None


try:
    stored = frappe.db.get_value("DefaultValue", {"parent": "__default", "defkey": KEY}, "defvalue")
    record = json.loads(stored) if stored else {}
    granted = removed = 0
    for email, member in sorted(wanted.items()):
        state = grant(email, member, record.get(email))
        granted += state != record.get(email) and state in ("made", "added")
        record[email] = state
    for email in sorted(set(record) - set(wanted)):
        removed += record[email] in ("made", "added")
        state = revoke(email, record[email])
        if state:
            record[email] = state
        else:
            del record[email]
    frappe.defaults.set_global_default(KEY, json.dumps(record, sort_keys=True))
    frappe.db.commit()
    print("catena access: customer: " + str(granted) + " granted, " + str(removed) + " removed", flush=True)
finally:
    frappe.destroy()
PY
