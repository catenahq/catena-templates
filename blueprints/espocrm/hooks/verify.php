function call($path) {
  $context = stream_context_create(['http' => ['timeout' => 20, 'ignore_errors' => true]]);
  $body = @file_get_contents('http://127.0.0.1' . $path, false, $context);
  $code = 0;
  if ($body !== false && isset($http_response_header[0]) && preg_match('/ (\d{3})/', $http_response_header[0], $m)) {
    $code = (int) $m[1];
  }
  return [$code, (string) $body];
}
[$code, $body] = call('/');
if ($code !== 200 || stripos($body, 'espo') === false) {
  fwrite(STDERR, 'EspoCRM answers ' . $code . ' on / without its page' . "\n");
  exit(1);
}
[$code] = call('/api/v1/App/user');
if ($code !== 401) {
  fwrite(STDERR, 'the EspoCRM API answers ' . $code . ', not the 401 of its own sign-in' . "\n");
  exit(1);
}
echo "EspoCRM answers on port 80 and its API asks for sign-in\n";
