const fail = (m) => {
  console.error(m);
  process.exit(1);
};
require('http').get({ host: '127.0.0.1', port: 5006, path: '/info', timeout: 10000 }, (res) => {
  let body = '';
  res.on('data', (d) => { body += d; });
  res.on('end', () => {
    let build = null;
    try { build = JSON.parse(body).build; } catch (e) {}
    if (res.statusCode !== 200 || !build || build.name !== '@actual-app/sync-server') {
      fail('Actual Budget /info answered ' + res.statusCode + ' without its build name');
    }
    console.log('Actual Budget answers /info: sync-server ' + build.version);
  });
}).on('timeout', function () {
  this.destroy(new Error('no answer in 10s'));
}).on('error', (e) => fail('Actual Budget does not answer on 5006: ' + e.message));
