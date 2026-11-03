import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  use: {
    baseURL: "http://127.0.0.1:5176",
    trace: "on-first-retry",
  },
  webServer: [
    {
      command:
        "cd .. && PYTHONPATH=. ROOTIQ_DATA_DIR=${TMPDIR:-/tmp}/rootiq-e2e-data ROOTIQ_ALLOWED_ORIGINS=http://127.0.0.1:5176 ROOTIQ_AUTH_USERNAME=e2e-agent ROOTIQ_AUTH_PASSWORD=e2e-password " +
        // no agent runs, SLA watcher, Jira or Gmail: this test checks the web flow without external calls
        "ROOTIQ_AGENT_AUTORUN=false ROOTIQ_AGENT_SLA_WATCH_SECONDS=0 ROOTIQ_JIRA_SITE_URL= ROOTIQ_JIRA_API_TOKEN= ROOTIQ_GMAIL_REFRESH_TOKEN= " +
        ".venv/bin/uvicorn backend.app.main:app --host 127.0.0.1 --port 8023",
      url: "http://127.0.0.1:8023/docs",
      reuseExistingServer: false,
    },
    {
      command:
        "ROOTIQ_API_TARGET=http://127.0.0.1:8023 npm run dev -- --host 127.0.0.1 --port 5176",
      url: "http://127.0.0.1:5176",
      reuseExistingServer: false,
    },
  ],
});
