$context = stream_context_create(['http' => [
  'timeout' => 20, 'ignore_errors' => true, 'follow_location' => 0,
  'header' => "X-Forwarded-Proto: https\r\n",
]]);
$body = @file_get_contents('http://nginx/', false, $context);
if ($body === false || !isset($http_response_header[0]) || !preg_match('/ (\d{3})/', $http_response_header[0], $m)) {
  fwrite(STDERR, 'WordPress does not answer through nginx' . "\n");
  exit(1);
}
$code = (int) $m[1];
$headers = strtolower(implode("\n", $http_response_header));
$wordpress = strpos($headers, 'api.w.org') !== false
  || strpos($headers, 'x-redirect-by: wordpress') !== false
  || strpos($body, 'wp-content') !== false;
if (!in_array($code, [200, 301, 302], true) || !$wordpress) {
  fwrite(STDERR, 'nginx answers ' . $code . ', but not with a WordPress page' . "\n");
  exit(1);
}
echo "WordPress answers through nginx and PHP\n";
