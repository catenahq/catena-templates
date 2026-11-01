body=$(curl -fsS --max-time 20 http://127.0.0.1/healthcheck) || {
  echo "ONLYOFFICE does not answer /healthcheck on port 80" >&2
  exit 1
}
if [ "$body" = true ]; then
  echo "ONLYOFFICE document server answers /healthcheck"
else
  echo "ONLYOFFICE /healthcheck answers $body, not true" >&2
  exit 1
fi
