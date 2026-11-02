const http = require("http");
const get = (host, port, path) => new Promise((resolve) => {
  const request = http.get({host, port, path, timeout: 15000}, (answer) => {
    let body = "";
    answer.on("data", (chunk) => { body += chunk; });
    answer.on("end", () => resolve([answer.statusCode, body]));
  });
  request.on("timeout", () => request.destroy(new Error("timed out")));
  request.on("error", (error) => resolve([0, String(error)]));
});
const end = Date.now() + 150000;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const ask = async (host, port, path) => {
  for (;;) {
    const answer = await get(host, port, path);
    const [status, body] = answer;
    const starting = status === 502 || status === 503 || (status === 0 && body.includes("ECONNREFUSED"));
    if (!starting || Date.now() >= end) return answer;
    await sleep(4000);
  }
};
(async () => {
  const failed = [];
  const [status, body] = await ask("127.0.0.1", 3000, "/api/info");
  let version = "";
  try { version = String(JSON.parse(body).version || ""); } catch (error) {}
  if (status !== 200 || !version) failed.push("/api/info answered " + (status ? "HTTP " + status : body.slice(0, 100)) + " with no version");
  const [jitsi] = await ask("rocketchat-oidc-jitsi", 80, "/");
  if (jitsi !== 200) failed.push("the Jitsi web server answered " + (jitsi ? "HTTP " + jitsi : "nothing"));
  if (failed.length) {
    console.error("the chat check failed: " + failed.join("; "));
    process.exit(1);
  }
  console.log("Rocket.Chat " + version + " answers and the Jitsi web server answers");
})();
