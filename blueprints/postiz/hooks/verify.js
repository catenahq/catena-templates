const end = Date.now() + 300000;
const NGINX = 5000;
const ORCHESTRATOR = 3002;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const once = (port, path) => new Promise((resolve, reject) => {
  require('http').get({ host: '127.0.0.1', port, path, timeout: 15000 }, (res) => {
    let body = '';
    res.on('data', (d) => { body += d; });
    res.on('end', () => resolve({ status: res.statusCode, body }));
  }).on('timeout', function () {
    const e = new Error('no answer in 15s');
    e.code = 'ETIMEDOUT';
    this.destroy(e);
  }).on('error', reject);
});
const get = async (port, path, again) => {
  for (;;) {
    let res;
    try {
      res = await once(port, path);
    } catch (e) {
      if (!again.codes.includes(e.code) || Date.now() >= end) throw e;
      await sleep(4000);
      continue;
    }
    if (again.statuses.includes(res.status) && Date.now() < end) {
      await sleep(4000);
      continue;
    }
    return res;
  }
};
const PROXY = { codes: ['ECONNREFUSED'], statuses: [502, 503] };
const HEALTH = { codes: ['ECONNREFUSED', 'ETIMEDOUT'], statuses: [500] };
(async () => {
  const api = await get(NGINX, '/api/', PROXY);
  if (api.status !== 200 || !api.body.includes('App is running')) {
    throw new Error('the backend answered ' + api.status + ' on /api/');
  }
  const front = await get(NGINX, '/', PROXY);
  if (front.status >= 400) {
    throw new Error('the front end answered ' + front.status);
  }
  const health = await get(ORCHESTRATOR, '/health/status', HEALTH);
  if (health.status !== 200 || !health.body.includes('"status":"ok"')) {
    throw new Error('the orchestrator answered ' + health.status + ' on /health/status: it does not reach Temporal');
  }
  console.log('Postiz backend and front end answer on 5000 and its orchestrator reaches Temporal');
})().catch((e) => {
  console.error('Postiz does not answer: ' + e.message);
  process.exit(1);
});
