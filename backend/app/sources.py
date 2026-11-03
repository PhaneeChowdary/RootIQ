"""Data sources: where the agents read logs, metrics, code changes, code and the service database.

The kinds of source:
- logs:     demo files in backend/seed/logs, Grafana Loki, Elasticsearch or OpenSearch, AWS CloudWatch Logs
- metrics:  Prometheus, optional
- changes:  the demo history, or GitHub commits and releases, optional
- code:     see code_sources.py, optional
- database: see db_sources.py, optional

Each log source does two things: it lists the services, and it gives the recent lines of one service, oldest first.
Each line starts with its time, for example 2026-10-03T19:53:02Z. The investigation tools filter these lines and
show the lines near each match. The guard against invented log lines checks quoted times against these lines.

The admin sets the sources on the Settings page. The app keeps the choice in backend/data/data_sources.json. Only the
owner of the file can read it, and the secret fields are encrypted. See crypto.py.
"""

import json
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from .crypto import decrypt, encrypt

TIMEOUT = 15
DEFAULT_WINDOW_HOURS = 72
DEFAULT_MAX_LINES = 2000


class SourceError(RuntimeError):
    """A source cannot answer: wrong address, wrong key, or the service is not there. The message says which."""


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _with_time(moment: datetime, line: str) -> str:
    """Put the time in front of a line that does not start with one."""
    return line if line[:4].isdigit() and "T" in line[:20] else f"{_stamp(moment)} {line}"


def _http_error(name: str, exc: Exception) -> SourceError:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return SourceError(f"{name} refused the credentials (HTTP {code}).")
        return SourceError(f"{name} answered with HTTP {code}.")
    return SourceError(f"Cannot reach {name}. Check the address. ({type(exc).__name__})")


# ---------------------------------------------------------------- logs

class FileLogSource:
    """Demo logs: one file for each service, for example payments-api.log."""

    name = "Demo files"

    def __init__(self, folder: Path) -> None:
        self.folder = Path(folder)

    def services(self) -> list[str]:
        return sorted(p.stem for p in self.folder.glob("*.log"))

    def lines(self, service: str) -> list[str]:
        path = self.folder / f"{service.strip().lower()}.log"
        if not path.is_file():
            raise SourceError(f"No logs for service '{service}'. Available services: {', '.join(self.services()) or 'none'}.")
        return path.read_text().splitlines()


class LokiLogSource:
    """Grafana Loki. The service is a stream label, service by default."""

    name = "Grafana Loki"

    def __init__(self, url: str, label: str = "service", user: str = "", password: str = "", token: str = "",
                 window_hours: int = DEFAULT_WINDOW_HOURS, max_lines: int = DEFAULT_MAX_LINES, client: httpx.Client | None = None) -> None:
        self.url = url.rstrip("/")
        self.label = label or "service"
        self.window = timedelta(hours=window_hours)
        self.max_lines = max_lines
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        auth = (user, password) if user else None
        self.client = client or httpx.Client(timeout=TIMEOUT, headers=headers, auth=auth)

    def _get(self, path: str, params: dict) -> dict:
        try:
            response = self.client.get(f"{self.url}{path}", params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            raise _http_error("Loki", exc) from exc

    def _range(self) -> dict:
        end = datetime.now(UTC)
        return {"start": int((end - self.window).timestamp() * 1e9), "end": int(end.timestamp() * 1e9)}

    def services(self) -> list[str]:
        return sorted(self._get(f"/loki/api/v1/label/{self.label}/values", self._range()).get("data") or [])

    def lines(self, service: str) -> list[str]:
        query = '{%s="%s"}' % (self.label, service.strip().replace('"', ""))
        data = self._get("/loki/api/v1/query_range",
                         {**self._range(), "query": query, "limit": self.max_lines, "direction": "backward"})
        entries = [(int(ts), line) for stream in data.get("data", {}).get("result", []) for ts, line in stream.get("values", [])]
        if not entries:
            known = self.services()
            if service not in known:
                raise SourceError(f"No logs for service '{service}' in Loki. Available services: {', '.join(known) or 'none'}.")
        entries.sort()
        return [_with_time(datetime.fromtimestamp(ts / 1e9, UTC), line) for ts, line in entries]


class ElasticLogSource:
    """Elasticsearch or OpenSearch. Field names follow the Elastic Common Schema by default."""

    name = "Elasticsearch"

    def __init__(self, url: str, index: str = "logs-*", service_field: str = "service.name", message_field: str = "message",
                 time_field: str = "@timestamp", level_field: str = "log.level", api_key: str = "", user: str = "",
                 password: str = "", window_hours: int = DEFAULT_WINDOW_HOURS, max_lines: int = DEFAULT_MAX_LINES,
                 client: httpx.Client | None = None) -> None:
        self.url = url.rstrip("/")
        self.index = index or "logs-*"
        self.fields = (service_field or "service.name", message_field or "message", time_field or "@timestamp", level_field or "log.level")
        self.window = timedelta(hours=window_hours)
        self.max_lines = max_lines
        headers = {"Authorization": f"ApiKey {api_key}"} if api_key else {}
        self.client = client or httpx.Client(timeout=TIMEOUT, headers=headers, auth=(user, password) if user else None)

    def _search(self, body: dict) -> dict:
        try:
            response = self.client.post(f"{self.url}/{self.index}/_search", json=body)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            raise _http_error("Elasticsearch", exc) from exc

    def _time_filter(self) -> dict:
        return {"range": {self.fields[2]: {"gte": _stamp(datetime.now(UTC) - self.window)}}}

    def services(self) -> list[str]:
        body = {"size": 0, "query": self._time_filter(), "aggs": {"s": {"terms": {"field": self.fields[0], "size": 200}}}}
        return sorted(b["key"] for b in self._search(body).get("aggregations", {}).get("s", {}).get("buckets", []))

    def lines(self, service: str) -> list[str]:
        service_field, message_field, time_field, level_field = self.fields
        body = {"size": self.max_lines, "sort": [{time_field: "desc"}],
                "query": {"bool": {"filter": [{"term": {service_field: service}}, self._time_filter()]}}}
        hits = self._search(body).get("hits", {}).get("hits", [])
        if not hits and service not in self.services():
            raise SourceError(f"No logs for service '{service}' in Elasticsearch. Available services: {', '.join(self.services()) or 'none'}.")
        out = []
        for hit in reversed(hits):
            doc = hit.get("_source", {})
            level = _dig(doc, level_field)
            out.append(f"{_dig(doc, time_field)} {str(level).upper() + ' ' if level else ''}{_dig(doc, message_field)}")
        return out


def _dig(doc: dict, dotted: str):
    """A field from a document, with or without nested objects: "service.name" or {"service": {"name": ...}}."""
    if dotted in doc:
        return doc[dotted]
    current = doc
    for part in dotted.split("."):
        current = current.get(part, "") if isinstance(current, dict) else ""
    return current


class CloudWatchLogSource:
    """AWS CloudWatch Logs. Each service is a log group: a prefix and the service name. The default prefix is /app/."""

    name = "AWS CloudWatch"

    def __init__(self, region: str, group_prefix: str = "/app/", access_key: str = "", secret_key: str = "",
                 window_hours: int = DEFAULT_WINDOW_HOURS, max_lines: int = DEFAULT_MAX_LINES, client=None) -> None:
        self.prefix = group_prefix if group_prefix.endswith("/") or not group_prefix else group_prefix + "/"
        self.window = timedelta(hours=window_hours)
        self.max_lines = max_lines
        if client is None:
            try:
                import boto3
            except ImportError as exc:
                raise SourceError("CloudWatch needs the boto3 package: .venv/bin/pip install boto3") from exc
            creds = {"aws_access_key_id": access_key, "aws_secret_access_key": secret_key} if access_key else {}
            client = boto3.client("logs", region_name=region, **creds)
        self.client = client

    def services(self) -> list[str]:
        try:
            groups = self.client.describe_log_groups(logGroupNamePrefix=self.prefix).get("logGroups", [])
        except Exception as exc:  # botocore raises many error types
            raise SourceError(f"CloudWatch refused the request: {str(exc)[:160]}") from exc
        return sorted(g["logGroupName"][len(self.prefix):] for g in groups)

    def lines(self, service: str) -> list[str]:
        start = int((datetime.now(UTC) - self.window).timestamp() * 1000)
        events, token = [], None
        try:
            while len(events) < self.max_lines:
                args = {"logGroupName": f"{self.prefix}{service}", "startTime": start, "limit": 1000}
                if token:
                    args["nextToken"] = token
                page = self.client.filter_log_events(**args)
                events += page.get("events", [])
                token = page.get("nextToken")
                if not token:
                    break
        except Exception as exc:
            if "ResourceNotFound" in type(exc).__name__ or "ResourceNotFound" in str(exc):
                raise SourceError(f"No log group '{self.prefix}{service}'. Available services: {', '.join(self.services()) or 'none'}.") from exc
            raise SourceError(f"CloudWatch refused the request: {str(exc)[:160]}") from exc
        events.sort(key=lambda e: e["timestamp"])
        return [_with_time(datetime.fromtimestamp(e["timestamp"] / 1000, UTC), e["message"].rstrip()) for e in events[-self.max_lines:]]


# ---------------------------------------------------------------- metrics

class PrometheusMetricSource:
    name = "Prometheus"

    def __init__(self, url: str, token: str = "", user: str = "", password: str = "", client: httpx.Client | None = None) -> None:
        self.url = url.rstrip("/")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.client = client or httpx.Client(timeout=TIMEOUT, headers=headers, auth=(user, password) if user else None)

    def _get(self, path: str, params: dict) -> dict:
        try:
            response = self.client.get(f"{self.url}{path}", params=params)
            body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
            if response.status_code == 400 and body.get("error"):
                raise SourceError(f"Prometheus cannot run that query: {body['error'][:200]}")
            response.raise_for_status()
            return body
        except httpx.HTTPError as exc:
            raise _http_error("Prometheus", exc) from exc

    def list_metrics(self, contains: str = "") -> list[str]:
        names = self._get("/api/v1/label/__name__/values", {}).get("data") or []
        return [n for n in names if contains.lower() in n.lower()]

    def query(self, promql: str, minutes: int = 60) -> list[dict]:
        """A short summary of each series over the last `minutes`: labels, first, last, lowest and highest value."""
        minutes = max(1, min(int(minutes), 7 * 24 * 60))
        end = datetime.now(UTC)
        step = max(15, minutes * 60 // 60)
        data = self._get("/api/v1/query_range", {"query": promql, "start": (end - timedelta(minutes=minutes)).timestamp(),
                                                  "end": end.timestamp(), "step": step})
        out = []
        for series in (data.get("data") or {}).get("result", [])[:15]:
            values = [(float(t), float(v)) for t, v in series.get("values", []) if v not in ("NaN", "+Inf", "-Inf")]
            if not values:
                continue
            nums = [v for _, v in values]
            peak = max(values, key=lambda tv: tv[1])
            out.append({"labels": series.get("metric", {}), "first": round(nums[0], 4), "last": round(nums[-1], 4),
                        "min": round(min(nums), 4), "max": round(max(nums), 4),
                        "max_at": _stamp(datetime.fromtimestamp(peak[0], UTC))})
        return out


# ---------------------------------------------------------------- changes

class GitHubChangeSource:
    """Recent commits and releases. `repos` maps a service to owner/repo; "*" is the repo for all other services."""

    name = "GitHub"

    def __init__(self, token: str, repos: dict[str, str], api: str = "https://api.github.com", client: httpx.Client | None = None) -> None:
        self.repos = repos
        self.api = api.rstrip("/")
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.client = client or httpx.Client(timeout=TIMEOUT, headers=headers)

    def _get(self, path: str, params: dict | None = None):
        try:
            response = self.client.get(f"{self.api}{path}", params=params or {})
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            raise _http_error("GitHub", exc) from exc

    def repo_for(self, service: str) -> str:
        repo = self.repos.get(service) or self.repos.get("*")
        if not repo:
            raise SourceError(f"No GitHub repository for service '{service}'. Known services: {', '.join(k for k in self.repos if k != '*') or 'none'}.")
        return repo

    def recent_changes(self, service: str, hours: int = 48) -> list[str]:
        repo = self.repo_for(service)
        since = datetime.now(UTC) - timedelta(hours=max(1, min(int(hours), 24 * 30)))
        lines = []
        for c in self._get(f"/repos/{repo}/commits", {"since": _stamp(since), "per_page": 30}):
            commit = c.get("commit", {})
            when = (commit.get("committer") or commit.get("author") or {}).get("date", "")
            author = (commit.get("author") or {}).get("name", "")
            lines.append(f"{when} commit {c.get('sha', '')[:7]} by {author}: {commit.get('message', '').splitlines()[0][:160]}")
        for r in self._get(f"/repos/{repo}/releases", {"per_page": 10}):
            when = r.get("published_at") or r.get("created_at") or ""
            if when and when >= _stamp(since):
                lines.append(f"{when} release {r.get('tag_name', '')}: {(r.get('name') or '').strip()[:120]}")
        return sorted(lines)


class DemoChangeSource:
    """Changes in the demo environment: a short history in backend/seed/changes.json, and the deploy, config and
    ops lines of the demo logs. Thus a simulated bad deploy and a rollback by the demo runner are changes too."""

    name = "Demo changes"
    EVENT = ("[deploy]", "[config]", "[ops]", "[flags]")

    def __init__(self, logs: FileLogSource, history: Path) -> None:
        self.logs = logs
        self.history = Path(history)
        self.repos = {}

    def recent_changes(self, service: str, hours: int = 48) -> list[str]:
        since = _stamp(datetime.now(UTC) - timedelta(hours=max(1, min(int(hours), 24 * 30))))
        out = []
        try:
            history = json.loads(self.history.read_text())
        except (FileNotFoundError, ValueError):
            history = []
        for item in history:
            if item["service"] == service and item["at"] >= since:
                out.append(f"{item['at']} {item['kind']} {item['id']} by {item['by']}: {item['text']}")
        try:
            lines = self.logs.lines(service)
        except SourceError:
            if not out and service not in self.logs.services():
                raise SourceError(f"No changes for service '{service}'. Known services: {', '.join(self.logs.services())}.") from None
            lines = []
        for line in lines:
            if any(tag in line for tag in self.EVENT) and line[:20] >= since[:20]:
                out.append(line)
        return sorted(set(out))


# ---------------------------------------------------------------- settings

FIELDS: dict[str, dict[str, list[tuple[str, str, bool]]]] = {
    # kind -> type -> []. Field, label, secret.
    "logs": {
        "demo": [],
        "loki": [("url", "Loki address", False), ("label", "Service label", False), ("user", "User", False),
                 ("password", "Password", True), ("token", "Bearer token", True)],
        "elasticsearch": [("url", "Address", False), ("index", "Index pattern", False), ("service_field", "Service field", False),
                          ("message_field", "Message field", False), ("time_field", "Time field", False),
                          ("level_field", "Level field", False), ("api_key", "API key", True), ("user", "User", False),
                          ("password", "Password", True)],
        "cloudwatch": [("region", "AWS region", False), ("group_prefix", "Log group prefix", False),
                       ("access_key", "Access key ID", False), ("secret_key", "Secret access key", True)],
    },
    "metrics": {"none": [], "prometheus": [("url", "Prometheus address", False), ("token", "Bearer token", True),
                                           ("user", "User", False), ("password", "Password", True)]},
    "changes": {"none": [], "demo": [], "github": [("token", "Access token", True), ("repos", "Repositories", False)]},
    "code": {"none": [], "demo": [("cloud", "Cloud models", False)],
             "local": [("root", "Folder", False), ("repos", "Folders by service", False), ("cloud", "Cloud models", False)],
             "github": [("token", "Read-only token", True), ("repos", "Repositories", False), ("cloud", "Cloud models", False)]},
}
FIELDS["database"] = {"none": [],
                      "postgres": [("url", "Address (read-only user)", True), ("services", "Services", False), ("refused", "Refused tables", False)],
                      "sqlite": [("path", "File", False), ("services", "Services", False), ("refused", "Refused tables", False)]}
DEFAULT_TYPE = {"logs": "demo", "metrics": "none", "changes": "demo", "code": "demo", "database": "none"}
KINDS = ("logs", "metrics", "changes", "code", "database")


def secret_fields(kind: str, type_: str) -> set[str]:
    return {name for name, _, secret in FIELDS[kind][type_] if secret}


def parse_repos(text: str) -> dict[str, str]:
    """Lines such as "payments-api = acme/payments" or "* = acme/monorepo"."""
    repos = {}
    for line in text.replace(",", "\n").splitlines():
        if "=" in line:
            service, repo = (part.strip() for part in line.split("=", 1))
            if service and repo:
                repos[service] = repo
    return repos


def build(kind: str, type_: str, config: dict, settings):
    """Make a source object from its saved settings. The secrets are already decrypted."""
    c = {k: v for k, v in config.items() if v not in (None, "")}
    if kind == "logs":
        if type_ == "demo":
            return FileLogSource(settings.logs_dir)
        if type_ == "loki":
            return LokiLogSource(c.get("url", ""), c.get("label", "service"), c.get("user", ""), c.get("password", ""), c.get("token", ""))
        if type_ == "elasticsearch":
            return ElasticLogSource(c.get("url", ""), c.get("index", "logs-*"), c.get("service_field", "service.name"),
                                    c.get("message_field", "message"), c.get("time_field", "@timestamp"),
                                    c.get("level_field", "log.level"), c.get("api_key", ""), c.get("user", ""), c.get("password", ""))
        if type_ == "cloudwatch":
            return CloudWatchLogSource(c.get("region", "us-east-1"), c.get("group_prefix", "/app/"), c.get("access_key", ""), c.get("secret_key", ""))
    if kind == "metrics":
        return None if type_ == "none" else PrometheusMetricSource(c.get("url", ""), c.get("token", ""), c.get("user", ""), c.get("password", ""))
    if kind == "changes":
        if type_ == "none":
            return None
        if type_ == "demo":
            return DemoChangeSource(FileLogSource(settings.logs_dir), Path(__file__).resolve().parent.parent / "seed" / "changes.json")
        return GitHubChangeSource(c.get("token", ""), parse_repos(c.get("repos", "")))
    if kind == "database":
        from .db_sources import DatabaseSource

        if type_ == "none":
            return None
        if type_ == "postgres":
            if not str(c.get("url", "")).startswith(("postgres://", "postgresql://")):
                raise SourceError("The address must start with postgresql://")
            return DatabaseSource("postgres", c["url"], c.get("services", "*"), c.get("refused", ""))
        if not Path(c.get("path", "")).expanduser().is_file():
            raise SourceError(f"No file {c.get('path', '')}.")
        return DatabaseSource("sqlite", str(Path(c["path"]).expanduser()), c.get("services", "*"), c.get("refused", ""))
    if kind == "code":
        from .code_sources import FolderCodeSource, GitHubCodeSource, parse_map

        if type_ == "none":
            return None
        if type_ == "demo":
            return FolderCodeSource(Path(__file__).resolve().parent.parent / "seed" / "code", label="Demo code")
        if type_ == "local":
            root = c.get("root", "")
            if root and not Path(root).expanduser().is_dir():
                raise SourceError(f"No folder {root}.")
            return FolderCodeSource(Path(root).expanduser() if root else None, parse_map(c.get("repos", "")), label="Local folder")
        return GitHubCodeSource(c.get("token", ""), parse_map(c.get("repos", "")))
    raise SourceError(f"Unknown source: {kind} {type_}")


def check(kind: str, source) -> str:
    """One small read that proves the source works. Returns a short result for the page."""
    if source is None:
        return "Off."
    if kind == "logs":
        services = source.services()
        return f"{len(services)} service(s): {', '.join(services[:8])}{'…' if len(services) > 8 else ''}" if services else "Connected, but no services in the time window."
    if kind == "metrics":
        return f"{len(source.list_metrics())} metric names."
    if isinstance(source, DemoChangeSource):
        return f"{len(source.logs.services())} demo services."
    if kind == "database":
        from .db_sources import DatabaseError

        try:
            return source.check()
        except DatabaseError as exc:
            raise SourceError(str(exc)) from exc
    if kind == "code":
        services = source.services()
        if not services:
            return "No services with code."
        files = source.list_files(services[0])
        return f"{len(services)} service(s) with code: {', '.join(services[:6])}. {services[0]}: {len(files)} files."
    repos = sorted(set(source.repos.values()))
    for repo in repos:
        source._get(f"/repos/{repo}")
    return f"{len(repos)} repository(ies) readable: {', '.join(repos)}" if repos else "No repositories set."


@dataclass
class Sources:
    logs: object
    metrics: object | None = None
    changes: object | None = None
    code: object | None = None
    code_cloud: bool = False  # True: cloud models can read the code; the "Cloud models" switch of the code source
    database: object | None = None


class SourceStore:
    """The saved choice for each kind. Secret fields are encrypted in the file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.lock = threading.Lock()

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            return {}

    def save_kind(self, kind: str, type_: str, config: dict) -> None:
        secrets = secret_fields(kind, type_)
        stored = {k: (encrypt(v) if k in secrets and v else v) for k, v in config.items()}
        with self.lock:
            data = self.load()
            data[kind] = {"type": type_, "config": stored}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            os.chmod(tmp, 0o600)
            tmp.replace(self.path)

    def get(self, kind: str) -> tuple[str, dict]:
        """The type and the settings of one kind, with secrets decrypted."""
        entry = self.load().get(kind) or {}
        type_ = entry.get("type") or DEFAULT_TYPE[kind]
        if type_ not in FIELDS[kind]:
            type_ = DEFAULT_TYPE[kind]
        secrets = secret_fields(kind, type_)
        return type_, {k: (decrypt(v) if k in secrets and v else v) for k, v in (entry.get("config") or {}).items()}


_stores: dict[Path, SourceStore] = {}


def source_store(settings) -> SourceStore:
    path = Path(settings.data_dir) / "data_sources.json"
    return _stores.setdefault(path, SourceStore(path))


def active_sources(settings) -> Sources:
    """The sources that the agents use now. If a saved source cannot be built, the demo logs are used."""
    if getattr(settings, "data_dir", None) is None:
        return Sources(FileLogSource(settings.logs_dir))
    store = source_store(settings)
    built = {}
    for kind in KINDS:
        type_, config = store.get(kind)
        try:
            built[kind] = build(kind, type_, config, settings)
        except SourceError:
            built[kind] = FileLogSource(settings.logs_dir) if kind == "logs" else None
    code_cloud = store.get("code")[1].get("cloud", "off") == "on"
    return Sources(built["logs"], built["metrics"], built["changes"], built["code"], code_cloud, built["database"])
