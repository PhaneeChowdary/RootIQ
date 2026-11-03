const { chromium } = require("@playwright/test");
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1600, height: 1250 }, deviceScaleFactor: 1 });
  await p.goto("http://127.0.0.1:3000/d/rootiq-agents/rootiq-agents?orgId=1&kiosk&from=now-30m&to=now", { waitUntil: "networkidle" });
  await p.waitForTimeout(6000);
  await p.screenshot({ path: process.argv[2], fullPage: true });
  const errs = await p.locator("text=/No data|Error|error/").allInnerTexts().catch(() => []);
  console.log("panel messages:", errs.slice(0, 10));
  await b.close();
})();
