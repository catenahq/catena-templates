const get = (path) => new Promise((resolve, reject) => {
  require('http').get({ host: '127.0.0.1', port: 2283, path, timeout: 15000 }, (res) => {
    let body = '';
    res.on('data', (d) => { body += d; });
    res.on('end', () => resolve({ status: res.statusCode, body }));
  }).on('timeout', function () {
    this.destroy(new Error('no answer in 15s'));
  }).on('error', reject);
});
(async () => {
  const ping = await get('/api/server/ping');
  if (ping.status !== 200 || JSON.parse(ping.body).res !== 'pong') {
    throw new Error('/api/server/ping answered ' + ping.status);
  }
  const v = JSON.parse((await get('/api/server/version')).body);
  if (typeof v.major !== 'number') {
    throw new Error('/api/server/version names no version');
  }
  console.log('Immich ' + v.major + '.' + v.minor + '.' + v.patch + ' answers');
})().catch((e) => {
  console.error('Immich does not answer on 2283: ' + e.message);
  process.exit(1);
});
