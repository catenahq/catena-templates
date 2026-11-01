import sys
import urllib.request

try:
    body = urllib.request.urlopen("http://app:8090/api/health", timeout=20).read().decode()
except Exception as e:
    print("the Beszel hub does not answer on port 8090: " + str(e), file=sys.stderr)
    sys.exit(1)
if "healthy" not in body:
    print("port 8090 answers, but not with the hub's health message", file=sys.stderr)
    sys.exit(1)
print("the Beszel hub answers its health endpoint")
