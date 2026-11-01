set -eu
case "$(tr '\0' ' ' < /proc/1/cmdline)" in
  bash\ *) echo "the scheduler has not started: its start waits for the backend" >&2; exit 1 ;;
  *schedule*) ;;
  *) echo "the scheduler is not the container's first process" >&2; exit 1 ;;
esac
exec /home/frappe/frappe-bench/env/bin/python - <<'PY'
import os
import sys
import urllib.request

from redis import Redis
from rq import Worker

site = os.environ["SITE_NAME"]
request = urllib.request.Request("http://backend:8000/api/method/ping", headers={"Host": site})
answer = urllib.request.urlopen(request, timeout=20).read().decode()
if "pong" not in answer:
    sys.exit("the site " + site + " did not answer pong: " + answer[:200])
workers = Worker.count(connection=Redis.from_url("redis://" + os.environ["REDIS_QUEUE"]))
if workers < 3:
    sys.exit("only " + str(workers) + " of 3 queue workers are registered")
print("site " + site + " answers, " + str(workers) + " queue workers registered")
PY
