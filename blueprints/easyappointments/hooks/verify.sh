out=$(curl -s --max-time 20 -w '\n%{http_code} %{redirect_url}' http://localhost/) || {
  echo "Easy!Appointments does not answer on port 80" >&2
  exit 1
}
meta=$(printf '%s\n' "$out" | tail -n 1)
code=${meta%% *}
where=${meta#* }
case "$code:$where" in
  200:*)
    case "$out" in
      *Easy!Appointments*) echo "Easy!Appointments answers on port 80" ;;
      *)
        echo "port 80 answers, but not with the Easy!Appointments page" >&2
        exit 1
        ;;
    esac
    ;;
  30[1278]:*/installation) echo "Easy!Appointments answers on port 80 and waits for its installation" ;;
  *)
    echo "port 80 answers status $code, but not with the Easy!Appointments page" >&2
    exit 1
    ;;
esac
