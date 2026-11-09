// n8n answers 503, or refuses the connection, while it connects to its database and runs its
// migrations: those answers are asked again every 3 s for 90 s, and the last one is judged.
const end = Date.now() + 90000;
const fail = (m) => {
  console.error(m);
  process.exit(1);
};
const again = (m) => {
  if (Date.now() >= end) fail(m);
  setTimeout(ask, 3000);
};
function ask() {
  require('http').get({ host: '127.0.0.1', port: 5678, path: '/healthz/readiness', timeout: 10000 }, (res) => {
    let body = '';
    res.on('data', (d) => { body += d; });
    res.on('end', () => {
      let status = null;
      try { status = JSON.parse(body).status; } catch (e) {}
      if (res.statusCode === 503) return again('n8n /healthz/readiness answered 503');
      if (res.statusCode !== 200 || status !== 'ok') {
        fail('n8n /healthz/readiness answered ' + res.statusCode);
      }
      console.log('n8n is ready: database connected, migrations done');
    });
  }).on('timeout', function () {
    this.destroy(new Error('no answer in 10s'));
  }).on('error', (e) => {
    if (e.code === 'ECONNREFUSED') return again('n8n does not answer on 5678: ' + e.message);
    fail('n8n does not answer on 5678: ' + e.message);
  });
}
ask();
