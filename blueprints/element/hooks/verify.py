import json
import os
import sys
import urllib.request

def get(path):
    request = urllib.request.Request("http://127.0.0.1:8008" + path, headers={"X-Forwarded-For": "127.0.0.1"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.read().decode()
    except Exception as e:
        sys.exit("Synapse does not answer " + path + " on 8008: " + str(e))

get("/health")
try:
    versions = json.loads(get("/_matrix/client/versions"))["versions"]
except (ValueError, KeyError, TypeError):
    versions = []
if not versions:
    sys.exit("Synapse /_matrix/client/versions lists no version")
turn = os.environ.get("TURN_HOSTNAME", "")
if turn:
    with open("/data/homeserver.yaml") as f:
        conf = f.read()
    if "turn:" + turn + ":" not in conf or "turn_shared_secret" not in conf:
        sys.exit("the call relay " + turn + " is provided, but homeserver.yaml does not name it")
print("Synapse answers, newest Matrix version " + versions[-1] + (", call relay " + turn + " wired" if turn else ""))
