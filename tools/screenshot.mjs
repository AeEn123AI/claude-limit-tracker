// Screenshots of the site in headless Firefox over WebDriver BiDi. No packages: Node 22+ and Firefox.
//   node tools/screenshot.mjs            -> screenshots/desktop-light.png, desktop-dark.png, phone.png
// Serves docs/ on a spare local port (python3 -m http.server), and fails if the page logs a script error.
import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync, rmSync, mkdtempSync } from "node:fs";
import { resolve, dirname, join } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const out = join(root, "screenshots");
const port = 8700 + Math.floor(Math.random() * 200);
const dport = port + 1000;
const base = `http://127.0.0.1:${port}/`;
mkdirSync(out, { recursive: true });
const profile = mkdtempSync(join(tmpdir(), "lt-ff-"));
writeFileSync(join(profile, "user.js"), [
  'user_pref("browser.shell.checkDefaultBrowser", false);', 'user_pref("datareporting.policy.dataSubmissionEnabled", false);',
  'user_pref("toolkit.telemetry.reportingpolicy.firstRun", false);', 'user_pref("browser.aboutwelcome.enabled", false);',
  'user_pref("app.update.enabled", false);',
].join("\n"));

const server = spawn("python3", ["-m", "http.server", String(port), "--bind", "127.0.0.1", "-d", join(root, "docs")], { stdio: "ignore" });
const ff = spawn("firefox", ["--headless", "--no-remote", "--profile", profile, "--remote-debugging-port", String(dport)], { stdio: "ignore", detached: true });
let failed = 0;
const done = (code) => {
  try { process.kill(-ff.pid); } catch (e) {}
  server.kill();
  setTimeout(() => { rmSync(profile, { recursive: true, force: true }); process.exit(code); }, 500);
};
setTimeout(() => { console.log("timed out"); done(1); }, 120_000);

let ws;
for (let i = 0; i < 60 && !ws; i++) {
  await sleep(500);
  ws = await new Promise((r) => { const w = new WebSocket(`ws://127.0.0.1:${dport}/session`); w.onopen = () => r(w); w.onerror = () => r(null); });
}
if (!ws) { console.log("Firefox didn't open its remote port"); done(1); }
let seq = 0;
const wait = new Map();
const errors = [];
ws.onmessage = (m) => {
  const d = JSON.parse(m.data);
  if (d.type === "event") { if (d.method === "log.entryAdded" && d.params.level === "error") errors.push(d.params.text); return; }
  const p = wait.get(d.id); if (!p) return; wait.delete(d.id);
  d.type === "error" ? p[1](new Error(d.message)) : p[0](d.result);
};
const cmd = (method, params = {}) => new Promise((ok, bad) => { const id = ++seq; wait.set(id, [ok, bad]); ws.send(JSON.stringify({ id, method, params })); });
const js = async (context, expression) => {
  const r = await cmd("script.evaluate", { expression: `(async () => JSON.stringify(await (${expression})))()`, target: { context }, awaitPromise: true });
  if (r.type !== "success") throw new Error("page script failed: " + JSON.stringify(r.exceptionDetails && r.exceptionDetails.text));
  return r.result.value === undefined ? undefined : JSON.parse(r.result.value);
};

await cmd("session.new", { capabilities: {} });
await cmd("session.subscribe", { events: ["log.entryAdded"] });

async function shot(name, w, h, theme) {
  const { context } = await cmd("browsingContext.create", { type: "window" });
  await cmd("browsingContext.setViewport", { context, viewport: { width: w, height: h } });
  await cmd("browsingContext.navigate", { context, url: base, wait: "complete" });
  await js(context, `(() => { localStorage.setItem("lt-theme", ${JSON.stringify(theme)}); return 1; })()`);
  await cmd("browsingContext.navigate", { context, url: base, wait: "complete" });
  let ok = false;
  for (let i = 0; i < 40 && !ok; i++) { await sleep(250); ok = await js(context, `document.querySelectorAll("#charts svg").length > 0`); }
  if (!ok) { console.log("  FAIL  charts did not render (" + name + ")"); failed++; }
  // open the first data table so it shows up in the shot
  await js(context, `(() => { const d = document.querySelector("#table-wrap details"); if (d) d.open = false; return 1; })()`);
  await sleep(500);
  const { data } = await cmd("browsingContext.captureScreenshot", { context, origin: "document" });
  writeFileSync(join(out, name + ".png"), Buffer.from(data, "base64"));
  console.log("saved screenshots/" + name + ".png");
  await cmd("browsingContext.close", { context }).catch(() => {});
}

try {
  await shot("desktop-light", 1280, 900, "light");
  await shot("desktop-dark", 1280, 900, "dark");
  await shot("phone", 390, 844, "light");
} catch (e) { console.log("  FAIL  " + e.message); failed++; }
if (errors.length) { console.log("  FAIL  page errors:\n    " + errors.join("\n    ")); failed++; }
done(failed ? 1 : 0);
