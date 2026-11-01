import json
import os
import sys
import uuid

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hc.settings")
sys.path.insert(0, os.getcwd())
import django

django.setup()

from django.contrib.auth import get_user_model
from hc.accounts.models import Profile, Project
from hc.api.models import Channel

# The folder's .env is this script's environment. A missing key raises KeyError, so a
# wiring break fails the deploy.
email = os.environ["ADMIN_EMAIL"]
password = os.environ["SUPERUSER_PASSWORD"]
ping_key = os.environ.get("PING_KEY", "")
read_key = os.environ.get("READ_KEY", "")
write_key = os.environ.get("API_KEY", "")
project_name = os.environ.get("HC_PROJECT_NAME", "")
mail_on = bool(os.environ.get("EMAIL_HOST", ""))
if not email:
    sys.exit("ADMIN_EMAIL is empty")

User = get_user_model()
login = email.lower()

# The superuser owns the project that holds the ping key, so an admin email change
# renames that one user. With no such project yet it is the user named after the
# admin email, created when missing. Keycloak sends the address in lower case and the
# sign-in gate's header is matched exactly, so the username and email are lower case;
# the email channel keeps the address as given. The password is set on creation only.
project = Project.objects.filter(ping_key=ping_key).first() if ping_key else None
operator = project.owner if project else (
    User.objects.filter(username=login).first()
    or User.objects.filter(email__iexact=login).first())
if operator is None:
    operator = User.objects.create_superuser(username=login, email=login, password=password)
else:
    # A gated visit that found no exact match made a second user on the lower case
    # address: its projects go to the operator and it is deleted. A user on any other
    # address is left alone.
    for dup in list(User.objects.filter(email__iexact=login).exclude(pk=operator.pk)):
        Project.objects.filter(owner=dup).update(owner=operator)
        dup.delete()
    wanted = {"username": login, "email": login, "is_superuser": True, "is_staff": True}
    changed = [f for f, v in wanted.items() if getattr(operator, f) != v]
    for field in changed:
        setattr(operator, field, wanted[field])
    if changed:
        operator.save(update_fields=changed)

# The theme is NULL until a person chooses; "" is a deliberate Light. Only NULL
# follows the browser's preference.
profile = Profile.objects.for_user(operator)
if profile.theme is None:
    profile.theme = "system"
    profile.save(update_fields=["theme"])

# Project.objects.create skips the sign-up flow's tutorial check and email channel.
if project is None:
    project = Project.objects.filter(owner=operator).first()
if project is None:
    project = Project.objects.create(owner=operator, name=project_name or "Catena")

# GET /api/v3/checks/ needs the read-only key; creating, updating, pausing and
# resuming a check need the full key. An empty value leaves the project's key as it is.
fields = ["name"]
if project_name:
    project.name = project_name
elif not project.name:
    project.name = "Catena"
for field, value in (("api_key_readonly", read_key), ("api_key", write_key), ("ping_key", ping_key)):
    if value:
        setattr(project, field, value)
        fields.append(field)
project.save(update_fields=fields)

# The admin's email is an alert channel while outgoing mail is on, notified on down
# and on up; it carries a fixed code, so a deploy updates or removes that one row
# and never a channel a person added. It is attached to every check when it is
# created and never again, so a check a person detaches it from stays detached. An email
# channel is marked verified: Healthchecks sends nothing to an unverified one.
code = uuid.uuid5(uuid.NAMESPACE_URL, "catena:healthchecks:admin-email")
if mail_on:
    channel, created = Channel.objects.update_or_create(
        project=project,
        code=code,
        defaults={
            "kind": "email",
            "value": json.dumps({"value": email, "up": True, "down": True}),
            "name": "Admin email",
            "email_verified": True,
        },
    )
    if created:
        channel.assign_all_checks()
    print("OK email=" + str(channel.code))
else:
    Channel.objects.filter(project=project, code=code).delete()
    print("OK email=none")
