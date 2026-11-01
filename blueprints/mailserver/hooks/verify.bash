set -u
fail=""
smtp() {
  exec 3<>/dev/tcp/127.0.0.1/25 || return 1
  IFS= read -r -t 10 line <&3 || return 1
  case "$line" in 220*) ;; *) return 2 ;; esac
  printf 'EHLO catena-verify\r\n' >&3
  while IFS= read -r -t 10 line <&3; do
    case "$line" in
      "250 "*) printf 'QUIT\r\n' >&3; return 0 ;;
      250-*) ;;
      *) return 3 ;;
    esac
  done
  return 3
}
( smtp ) 2>/dev/null
case $? in
  0) ;;
  1) fail="$fail smtp-25-does-not-answer" ;;
  2) fail="$fail smtp-25-sends-no-banner" ;;
  *) fail="$fail smtp-25-refuses-ehlo" ;;
esac
greeting=$(printf 'a1 LOGOUT\r\n' | timeout 20 openssl s_client -quiet -connect 127.0.0.1:993 2>/dev/null | head -n 1)
case "$greeting" in
  "* OK"*) ;;
  *) fail="$fail imap-993-sends-no-greeting" ;;
esac
if [ -n "${ANTIVIRUS_HOST:-}" ]; then
  verdict=$(printf '%s%s\n' 'X5O!P%@AP[4\PZX54(P^)7CC)7}' '$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*' | rspamc 2>/dev/null || true)
  printf '%s' "$verdict" | grep -q CLAM_VIRUS || fail="$fail eicar-not-detected"
fi
if [ -n "$fail" ]; then
  echo "the mail server check failed:$fail" >&2
  exit 1
fi
msg="SMTP on 25 answers its banner and EHLO and IMAP on 993 its greeting"
[ -z "${ANTIVIRUS_HOST:-}" ] || msg="$msg, and rspamd detects the EICAR test file through the virus scanner"
echo "$msg"
