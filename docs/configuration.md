# Configuration

All settings are in `backend/.env`. The template is `backend/.env.example`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `ROOTIQ_AGENT_MODEL` | `qwen3:8b` | Local model for the agents. It is the backup when another model is the main model |
| `ROOTIQ_GEMINI_API_KEY` | empty | Google AI Studio key. When you set it, Gemini is the main model until you select another on the Models page |
| `ROOTIQ_CODE_MODEL` | `qwen3:8b` | Local model for the Code agent. Another model, for example `qwen2.5-coder:7b`, goes first, then `ROOTIQ_AGENT_MODEL` |
| `ROOTIQ_GROQ_API_KEY` | empty | Groq key. Groq then answers the complex tasks first |
| `ROOTIQ_GROQ_MODEL` | `openai/gpt-oss-120b` | Groq model |
| `ROOTIQ_GROQ_DAILY_TOKENS` | `200000` | Daily token budget for Groq; 0 = none. Above 70%, only complex tasks use it |
| `ROOTIQ_OPENAI_DAILY_TOKENS` | `100000` | Daily token budget for OpenAI, which writes the Code agent's patches; 0 = none |
| `ROOTIQ_GEMINI_MODEL` | `gemini-3.5-flash` | Gemini model when the key comes from `.env` |
| `ROOTIQ_AGENT_PROVIDER` | `auto` | `ollama` = the local model only, for all tasks, when nothing is selected on the Models page |
| `ROOTIQ_AGENT_MAX_STEPS` | `8` | Tool rounds for the single-agent baseline |
| `ROOTIQ_AGENT_TIME_LIMIT_SECONDS` | `600` | Time limit for each team run; 0 = no limit |
| `ROOTIQ_AGENT_DRY_RUN` | `true` | The app records approved actions but does not send them |
| `ROOTIQ_AGENT_AUTORUN` | `true` | Start runs on new cases, Jira imports and SLA risk |
| `ROOTIQ_AGENT_CONCURRENCY` | `1` | Number of investigations at the same time |
| `ROOTIQ_AGENT_SLA_WATCH_SECONDS` | `60` | Time between SLA checks; 0 = off |
| `ROOTIQ_AGENT_SLA_COOLDOWN_MINUTES` | `120` | Maximum one SLA run per case in this time |
| `ROOTIQ_API_URL` | `http://127.0.0.1:8011` | Address that the MCP server uses for the API |
| `ROOTIQ_LOGS_DIR` | `backend/seed/logs` | Demo service logs, for the "Demo files" log source |
| `ROOTIQ_OIDC_ISSUER` | empty | Single sign-on provider, for example `https://accounts.google.com`. Empty = off |
| `ROOTIQ_OIDC_CLIENT_ID`, `_CLIENT_SECRET` | empty | The app's client at the provider |
| `ROOTIQ_OIDC_REDIRECT_URL` | `http://127.0.0.1:5174/api/auth/oidc/callback` | Register this address at the provider |
| `ROOTIQ_OIDC_NAME` | `SSO` | Button text: "Sign in with ..." |
| `ROOTIQ_OIDC_ALLOWED_DOMAINS` | empty | Email domains that get an account at the first sign-in |
| `ROOTIQ_OIDC_DEFAULT_ROLE` | `viewer` | Role of those new accounts |
| `ROOTIQ_PRIVACY_MASKING` | `cloud` | `cloud`: mask personal data before each cloud model call. `off`: do not mask |
| `ROOTIQ_LOGIN_MAX_FAILURES`, `_LOGIN_LOCK_MINUTES` | `5`, `15` | Failed sign-ins before a lock, and the lock time |
| `ROOTIQ_RETENTION_BIN_DAYS` | `30` | Empty the recycle bin after this many days; 0 = keep |
| `ROOTIQ_RETENTION_RESOLVED_DAYS` | `0` | Delete resolved cases after this many days; 0 = keep |
| `ROOTIQ_RETENTION_AUDIT_DAYS` | `0` | Delete audit events after this many days; 0 = keep |
| `ROOTIQ_ALLOWED_ORIGINS` | the local website addresses | Websites that can send changes to the API |
| `ROOTIQ_COOKIE_SECURE` | `false` | Set `true` when the site uses HTTPS |
| `ROOTIQ_DATABASE_URL` | empty | `postgresql://user:password@host:5432/db`. Empty = SQLite in `backend/data` |
| `ROOTIQ_AGENT_EXTERNAL_WORKERS` | `false` | `true` when worker processes run |
| `ROOTIQ_BACKUP_HOURS`, `_BACKUP_KEEP`, `_BACKUP_DIR` | `24`, `14`, `backend/data/backups` | Automatic backups by the API; 0 = off |
| `ROOTIQ_SECRET_KEY` | made in `backend/data/secret.key` | Key that encrypts the saved data-source secrets. Keep a copy |
| `ROOTIQ_OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama address |
| `ROOTIQ_OLLAMA_MODEL` | `qwen3:8b` | Model for the one-prompt analysis when the main model is local |
| `ROOTIQ_EMBEDDING_MODEL` | `nomic-embed-text` | Embeddings for incident memory and lessons |
| `ROOTIQ_ALERT_TOKEN` | empty | Grafana sends it with each alert. Empty = alert intake is off |
| `ROOTIQ_GRAFANA_PASSWORD` | empty | Grafana admin password. Viewing needs no sign-in |
| `ROOTIQ_GRAFANA_URL` | `http://127.0.0.1:3000` | Grafana address for the "Open in Grafana" links |
| `ROOTIQ_TIME_ZONE` | empty | IANA zone, such as `America/New_York`, for the times on the website, the daily token counts and the postmortem. Empty = this computer's zone |

Jira and Gmail are optional. Relative paths start from the project folder.

Two commands test the settings. They do not change data.

- `.venv/bin/python -m backend.app.jira_check` tests the Jira site, the sign-in, the project, the issue type and the
  priority field.
- `.venv/bin/python -m backend.app.model_check` lists the models of the key and tests a plain reply, a tool call and
  a structured reply.
