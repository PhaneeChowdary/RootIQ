// Records a captioned walkthrough of the agent features against a running app (no clicks that change data).
//   node e2e/demo-recording.cjs http://127.0.0.1:5174 ../reports/demo   (credentials from backend/.env)
const { chromium } = require("@playwright/test");
const fs = require("fs");
const path = require("path");

const BASE = process.argv[2] || "http://127.0.0.1:5173";
const OUT = path.resolve(process.argv[3] || "../reports/demo");
const env = Object.fromEntries(fs.readFileSync(path.join(__dirname, "../../backend/.env"), "utf8")
  .split("\n").filter((l) => l.startsWith("ROOTIQ_AUTH_")).map((l) => [l.split("=")[0], l.slice(l.indexOf("=") + 1)]));

async function caption(page, text, ms = 3200) {
  await page.evaluate((t) => {
    let el = document.getElementById("demo-caption");
    if (!el) {
      el = document.createElement("div");
      el.id = "demo-caption";
      Object.assign(el.style, {
        position: "fixed", left: "50%", bottom: "28px", transform: "translateX(-50%)", zIndex: 9999,
        maxWidth: "860px", padding: "14px 22px", borderRadius: "14px", background: "rgba(17,17,17,0.92)",
        color: "white", font: "600 17px/1.45 system-ui, -apple-system, sans-serif", textAlign: "center",
        boxShadow: "0 10px 30px rgba(0,0,0,0.25)",
      });
      document.body.appendChild(el);
    }
    el.textContent = t;
  }, text);
  await page.waitForTimeout(ms);
}

async function scrollTo(page, selector, ms = 1200) {
  await page.locator(selector).first().evaluate((el) => el.scrollIntoView({ behavior: "smooth", block: "start" }));
  await page.waitForTimeout(ms);
}

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch();
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    recordVideo: { dir: OUT, size: { width: 1440, height: 900 } },
  });
  const page = await context.newPage();
  await page.goto(BASE);
  await page.locator("form.login input").nth(0).fill(env.ROOTIQ_AUTH_USERNAME);
  await page.locator("form.login input[type=password]").fill(env.ROOTIQ_AUTH_PASSWORD);
  await page.locator("form.login").getByRole("button", { name: "Sign in" }).click();
  await page.locator(".case-tab").first().waitFor();
  await caption(page, "RootIQ: a LangGraph team of AI agents that investigates production incidents, running locally.", 4200);

  await page.locator(".case-tab", { hasText: "Dashboard shows blank panels" }).first().click();
  await page.locator(".detail-tab", { hasText: "Agent team" }).click();
  await page.locator(".agent-panel").waitFor();
  await caption(page, "A run started for this incident. The lead agent planned hypotheses and gave each specialist a task.", 4200);
  await scrollTo(page, ".agent-perf");
  await caption(page, "Observability: evidence and history agents ran in parallel. Every model call, token and tool call is recorded.", 4800);
  await scrollTo(page, ".agent-steps");
  await caption(page, "The trace: each tool call by each agent, with arguments, results and timing.", 3600);
  await page.locator(".agent-steps details").first().click();
  await page.waitForTimeout(1600);
  await scrollTo(page, ".agent-review");
  await caption(page, "A reviewer agent checks every claim against the raw tool output and can send work back.", 4200);
  await scrollTo(page, ".agent-proposal");
  await caption(page, "Nothing leaves the system on its own: the run is paused (LangGraph interrupt) for the engineer to approve, edit or reject.", 5200);

  await page.locator(".detail-tab", { hasText: "Analysis" }).click();
  await page.waitForTimeout(800);
  await caption(page, "The team's analysis lands in the normal review workflow, citing the exact log lines.", 4000);

  await page.locator(".header-nav").getByRole("button", { name: "Insights" }).click();
  await page.locator(".agent-metrics").waitFor();
  await scrollTo(page, ".agent-metrics", 800);
  await caption(page, "Across runs: investigation time, tokens, reviewer rejections, engineer decisions and diagnosis accuracy.", 4800);
  await caption(page, "Also: event-driven runs, crash recovery from checkpoints, lessons from resolved incidents, an MCP server and an eval suite.", 5200);

  await context.close();
  await browser.close();
  const video = fs.readdirSync(OUT).filter((f) => f.endsWith(".webm")).map((f) => path.join(OUT, f))
    .sort((a, b) => fs.statSync(b).mtimeMs - fs.statSync(a).mtimeMs)[0];
  fs.renameSync(video, path.join(OUT, "rootiq-demo.webm"));
  console.log("saved", path.join(OUT, "rootiq-demo.webm"));
})().catch((e) => { console.error(e); process.exit(1); });
