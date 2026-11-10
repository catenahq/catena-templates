const fail = (m) => {
  console.error(m);
  process.exit(1);
};
const base = 'http://127.0.0.1:3000';
const script = '/' + String(process.env.TRACKER_SCRIPT_NAME).split(',')[0].trim().replace(/^\/+/, '');
const collect = process.env.COLLECT_API_ENDPOINT;
const call = (path, init) => fetch(base + path, { ...init, signal: AbortSignal.timeout(15000) });
(async () => {
  const heartbeat = await call('/api/heartbeat');
  const reply = await heartbeat.json().catch(() => null);
  if (heartbeat.status !== 200 || !reply || reply.ok !== true) {
    fail('Umami /api/heartbeat answered ' + heartbeat.status + ' without ok');
  }
  const tracker = await call(script);
  const body = await tracker.text();
  if (tracker.status !== 200 || !collect || !body.includes(collect)) {
    fail('Umami ' + script + ' answered ' + tracker.status + ' without the collect path ' + collect);
  }
  const login = await call('/api/auth/login', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ username: 'admin', password: 'umami' }),
  });
  if (login.status !== 401) {
    fail('Umami sign-in as admin with the default password answered ' + login.status + ', expected 401');
  }
  console.log('Umami answers its heartbeat and ' + script + ' and refuses the default password');
})().catch((e) => fail('Umami does not answer on 3000: ' + e.message));
