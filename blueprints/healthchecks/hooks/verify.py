import sys
import urllib.request

request = urllib.request.Request("http://127.0.0.1:8000/api/v3/status/", headers={"Host": "localhost"})
try:
    body = urllib.request.urlopen(request, timeout=20).read().decode().strip()
except Exception as e:
    print("Healthchecks does not answer its status endpoint: " + str(e), file=sys.stderr)
    sys.exit(1)
if body != "OK":
    print("the status endpoint answered " + body[:80], file=sys.stderr)
    sys.exit(1)
print("Healthchecks answers and reads its database")
