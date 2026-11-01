out=$(clamdscan --no-summary - 2>&1)
rc=$?
case "$rc" in
0) ;;
1) printf '%s\n' "$out" | sed -n 's/^stream: \(.*\) FOUND$/FLAGGED \1/p' ;;
*) printf '%s\n' "$out" ;;
esac
exit "$rc"
