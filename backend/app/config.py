import os
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path

from dotenv import load_dotenv

BACKEND_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(BACKEND_ROOT / ".env")


def _env(name: str, fallback: str = "") -> str:
    return os.environ.get(f"ROOTIQ_{name}", fallback)


def _path(name: str, fallback: Path) -> Path:
    """The app reads relative paths from the project folder, not from the working directory. Thus tools that start
    from other places, for example an MCP client or a scheduler, find the same data."""
    path = Path(_env(name, str(fallback)))
    return path if path.is_absolute() else (BACKEND_ROOT.parent / path).resolve()


def zone(name: str) -> tzinfo:
    """The time zone for the day of the token budgets and for the times that people read. The service logs stay in
    UTC, as real logs are. Empty: the zone of this computer."""
    if name:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    return datetime.now().astimezone().tzinfo


@dataclass(frozen=True)
class Settings:
    data_dir: Path = _path("DATA_DIR", BACKEND_ROOT / "data")
    database_url: str = _env("DATABASE_URL")  # postgresql://user:password@host:5432/db; empty = SQLite in data_dir
    ollama_url: str = _env("OLLAMA_URL", "http://127.0.0.1:11434")
    ollama_model: str = _env("OLLAMA_MODEL", "qwen3:8b")
    embedding_model: str = _env("EMBEDDING_MODEL", "nomic-embed-text")
    jira_site_url: str = _env("JIRA_SITE_URL")
    jira_email: str = _env("JIRA_EMAIL")
    jira_api_token: str = _env("JIRA_API_TOKEN")
    jira_project_key: str = _env("JIRA_PROJECT_KEY")
    jira_issue_type: str = _env("JIRA_ISSUE_TYPE", "Task")
    jira_sync_jql: str = _env("JIRA_SYNC_JQL", "status != Done ORDER BY created DESC")
    gmail_client_id: str = _env("GMAIL_CLIENT_ID")
    gmail_client_secret: str = _env("GMAIL_CLIENT_SECRET")
    gmail_refresh_token: str = _env("GMAIL_REFRESH_TOKEN")
    auth_username: str = _env("AUTH_USERNAME", "support-admin")
    auth_password: str = _env("AUTH_PASSWORD", "rootiq-local-demo")
    auth_session_hours: int = int(_env("AUTH_SESSION_HOURS", "8"))
    oidc_issuer: str = _env("OIDC_ISSUER")  # single sign-on, see services/oidc.py; empty = off
    oidc_client_id: str = _env("OIDC_CLIENT_ID")
    oidc_client_secret: str = _env("OIDC_CLIENT_SECRET")
    oidc_redirect_url: str = _env("OIDC_REDIRECT_URL", "http://127.0.0.1:5174/api/auth/oidc/callback")
    oidc_name: str = _env("OIDC_NAME", "SSO")  # the button says "Sign in with <name>"
    oidc_allowed_domains: str = _env("OIDC_ALLOWED_DOMAINS")  # new users from these email domains get an account
    oidc_default_role: str = _env("OIDC_DEFAULT_ROLE", "viewer")
    grafana_url: str = _env("GRAFANA_URL", "http://127.0.0.1:3000")  # for the "Open in Grafana" links
    time_zone: str = _env("TIME_ZONE")  # such as America/New_York: "today" and the times that people read; empty = this computer's
    alert_token: str = _env("ALERT_TOKEN")  # Grafana sends it with each alert; empty = alert intake is off
    agent_model: str = _env("AGENT_MODEL", "qwen3:8b")
    code_model: str = _env("CODE_MODEL", "qwen3:8b")  # local model for the code agent; a different model goes before agent_model
    gemini_api_key: str = _env("GEMINI_API_KEY")
    gemini_model: str = _env("GEMINI_MODEL", "gemini-3.5-flash")
    groq_api_key: str = _env("GROQ_API_KEY")
    groq_model: str = _env("GROQ_MODEL", "openai/gpt-oss-120b")  # Groq: for the complex agent tasks
    groq_daily_tokens: int = int(_env("GROQ_DAILY_TOKENS", "200000"))  # Groq free tier; 0 = no budget
    gemini_daily_tokens: int = int(_env("GEMINI_DAILY_TOKENS", "0"))
    # OpenAI writes only the Code agent's patches; a paid key, so a daily cap. 0 = no budget
    openai_daily_tokens: int = int(_env("OPENAI_DAILY_TOKENS", "100000"))
    agent_provider: str = _env("AGENT_PROVIDER", "auto").lower()  # "ollama" = local model only
    agent_max_steps: int = int(_env("AGENT_MAX_STEPS", "8"))
    agent_time_limit_seconds: int = int(_env("AGENT_TIME_LIMIT_SECONDS", "600"))  # 0 = no limit
    agent_dry_run: bool = _env("AGENT_DRY_RUN", "true").lower() != "false"
    agent_autorun: bool = _env("AGENT_AUTORUN", "true").lower() != "false"
    agent_concurrency: int = int(_env("AGENT_CONCURRENCY", "1"))  # worker threads in the API process; 0 = none
    agent_external_workers: bool = _env("AGENT_EXTERNAL_WORKERS", "false").lower() == "true"  # true when separate worker processes run the agents
    agent_sla_watch_seconds: int = int(_env("AGENT_SLA_WATCH_SECONDS", "60"))
    agent_sla_cooldown_minutes: int = int(_env("AGENT_SLA_COOLDOWN_MINUTES", "120"))
    privacy_masking: str = _env("PRIVACY_MASKING", "cloud").lower()  # "cloud": mask personal data for cloud models; "off"
    login_max_failures: int = int(_env("LOGIN_MAX_FAILURES", "5"))  # failed sign-ins before a lock
    login_lock_minutes: int = int(_env("LOGIN_LOCK_MINUTES", "15"))
    retention_resolved_days: int = int(_env("RETENTION_RESOLVED_DAYS", "0"))  # delete resolved cases after N days; 0 = keep
    retention_bin_days: int = int(_env("RETENTION_BIN_DAYS", "30"))  # empty the recycle bin after N days; 0 = keep
    retention_audit_days: int = int(_env("RETENTION_AUDIT_DAYS", "0"))  # delete audit events after N days; 0 = keep
    backup_dir: str = _env("BACKUP_DIR")  # default: data_dir/backups
    backup_keep: int = int(_env("BACKUP_KEEP", "14"))
    backup_hours: int = int(_env("BACKUP_HOURS", "24"))  # the API makes a backup this often; 0 = off
    allowed_origins: str = _env("ALLOWED_ORIGINS", "http://127.0.0.1:5173,http://localhost:5173,http://127.0.0.1:5174,http://localhost:5174")
    cookie_secure: bool = _env("COOKIE_SECURE", "false").lower() == "true"  # true when the site uses HTTPS
    logs_dir: Path = _path("LOGS_DIR", BACKEND_ROOT / "seed" / "logs")
    api_url: str = _env("API_URL", "http://127.0.0.1:8011")  # used by the MCP server to reach this API

    @property
    def database_path(self) -> Path:
        return self.data_dir / "rootiq.db"

    @property
    def database_target(self):
        """ROOTIQ_DATABASE_URL, a postgresql:// address, when it is set. Else the SQLite file."""
        return self.database_url or self.database_path

    @property
    def checkpoint_path(self) -> Path:
        return self.data_dir / "agent_checkpoints.db"
