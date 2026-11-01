const fail = (m) => {
  console.error(m);
  process.exit(1);
};
const req = require('http').request({
  host: '127.0.0.1', port: 3000, path: '/api/auth.config', method: 'POST', timeout: 15000,
  headers: { 'Content-Type': 'application/json', 'X-Forwarded-Proto': 'https' },
}, (res) => {
  let body = '';
  res.on('data', (d) => { body += d; });
  res.on('end', () => {
    let reply = null;
    try { reply = JSON.parse(body); } catch (e) {}
    if (res.statusCode !== 200 || !reply || reply.ok !== true || typeof reply.data !== 'object') {
      fail('Outline /api/auth.config answered ' + res.statusCode + ' without its configuration');
    }
    console.log('Outline answers /api/auth.config');
  });
});
req.on('timeout', () => req.destroy(new Error('no answer in 15s')));
req.on('error', (e) => fail('Outline does not answer on 3000: ' + e.message));
req.end('{}');
