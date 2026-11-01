const fail = (m) => {
  console.error(m);
  process.exit(1);
};
require('http').get({ host: '127.0.0.1', port: 5678, path: '/healthz/readiness', timeout: 10000 }, (res) => {
  let body = '';
  res.on('data', (d) => { body += d; });
  res.on('end', () => {
    let status = null;
    try { status = JSON.parse(body).status; } catch (e) {}
    if (res.statusCode !== 200 || status !== 'ok') {
      fail('n8n /healthz/readiness answered ' + res.statusCode);
    }
    console.log('n8n is ready: database connected, migrations done');
  });
}).on('timeout', function () {
  this.destroy(new Error('no answer in 10s'));
}).on('error', (e) => fail('n8n does not answer on 5678: ' + e.message));
