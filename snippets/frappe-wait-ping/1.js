const http = require("http");
const site = process.env.SITE_NAME;
const deadline = Date.now() + 120000;
let last = "no answer";
const ping = () => new Promise((resolve) => {
  const request = http.get({host: "127.0.0.1", port: 8000, path: "/api/method/ping",
                            headers: {Host: site}, timeout: 10000}, (answer) => {
    let body = "";
    answer.on("data", (chunk) => { body += chunk; });
    answer.on("end", () => resolve([answer.statusCode, body]));
  });
  request.on("timeout", () => request.destroy(new Error("timed out")));
  request.on("error", (error) => resolve([0, String(error)]));
});
(async () => {
  while (Date.now() < deadline) {
    const [status, body] = await ping();
    if (status === 200 && body.includes("pong")) process.exit(0);
    last = status ? "HTTP " + status : body;
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
  console.error("the site " + site + " does not answer its ping after 120 s: " + last);
  process.exit(1);
})();
