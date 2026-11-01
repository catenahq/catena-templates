import json
import os
import sys
import time
import urllib.request

end = time.monotonic() + 330

def starting(e):
    return isinstance(getattr(e, "reason", None), ConnectionRefusedError) or getattr(e, "code", 0) in (502, 503)

def get(url):
    while True:
        try:
            with urllib.request.urlopen(url, timeout=15) as response:
                return response.status, response.read().decode()
        except Exception as e:
            if not starting(e) or time.monotonic() >= end:
                sys.exit("Plane does not answer " + url + ": " + str(e))
        time.sleep(4)

status, body = get("http://127.0.0.1:8000/api/instances/")
try:
    answer = json.loads(body)
except ValueError:
    answer = None
if status != 200 or not isinstance(answer, dict) or "instance" not in answer:
    sys.exit("Plane /api/instances/ answered " + str(status) + " without its instance record")
line = "Plane API answers"
store = os.environ.get("AWS_S3_ENDPOINT_URL", "")
if store and os.environ.get("USE_MINIO") == "1":
    get(store.rstrip("/") + "/minio/health/live")
    line += ", the object store is alive"
print(line)
