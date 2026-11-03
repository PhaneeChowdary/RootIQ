"""Read-only access to the code of each service, for the Code agent.

Three kinds of code source, on Settings > Data sources > Code:
- demo:   backend/seed/code/<service>/, the demo code that matches the demo incidents
- local:  a folder on this computer: <root>/<service>/, or a folder for each service: "payments-api = /src/pay"
- github: GitHub repositories, read with a read-only token: "payments-api = acme/payments", "* = acme/mono"

Nothing here can write. The rules for each source:
- A path must stay inside the code of the service. The app refuses "..", absolute paths and links out of the folder.
- The app never reads or lists secret files: .env files, private keys, certificates with keys, credentials files,
  token files and cloud service-account keys. It also never reads inside .git, virtual environments, node_modules or
  other hidden folders: .git/config can hold a token.
- Secrets that are written in the code itself, for example API_KEY = "sk-...", become [secret] before any agent
  sees them. The agent still sees where the code reads a secret, for example os.environ["API_KEY"].
- Nothing writes: the sources open files only to read them. A patch from the Code agent is a proposal for a person.
- Limits: the app does not read files over 200 KB. At most 300 lines in one read, 40 search results, 400 listed files.
"""

import base64
import fnmatch
import re
from pathlib import Path, PurePosixPath

import httpx

MAX_FILE_BYTES = 200_000
MAX_LINES = 300
MAX_RESULTS = 40
MAX_FILES = 400
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next", "target", "vendor"}
SECRET_NAMES = [".env", ".env.*", "*.env", ".envrc", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore", "id_rsa*", "id_ed25519*",
                "*secret*", "*credential*", "*.kdbx", ".npmrc", ".pypirc", ".netrc", ".pgpass", ".htpasswd", ".dockercfg",
                "*.pem", "token.json", "*token*.json", "*service-account*", "*service_account*", "*.tfvars", "*.tfstate",
                "*.tfstate.*", "kubeconfig", "*.kubeconfig", "*.ovpn", "*.sqlite", "*.sqlite3", "*.db"]
OPEN_HIDDEN_DIRS = {".github"}  # the only hidden folder that the agent may read: CI workflows
TEXT_SUFFIXES = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".java", ".kt", ".rb", ".rs", ".cs", ".php", ".c", ".h",
                 ".cpp", ".hpp", ".swift", ".scala", ".sql", ".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".conf",
                 ".properties", ".xml", ".md", ".txt", ".sh", ".tf", ".hcl", ".gradle", ".crt", ".html", ".css", ""}
TEXT_NAMES = {"Dockerfile", "Makefile", "Procfile", "Jenkinsfile", "Gemfile", "requirements.txt", "go.mod", "package.json"}


class CodeError(RuntimeError):
    """A code request that cannot be answered. The message says why."""


def is_secret(path: str) -> bool:
    """A file that can hold secrets, by its name or the name of a folder on its path, for example secrets/db.yaml."""
    return any(fnmatch.fnmatch(part.lower(), pattern) for part in PurePosixPath(path).parts for pattern in SECRET_NAMES)


def in_closed_folder(path: str) -> bool:
    """A path inside .git, .venv, node_modules or another hidden or skipped folder."""
    return any(part in SKIP_DIRS or part.startswith(".") and part not in OPEN_HIDDEN_DIRS
               for part in PurePosixPath(path).parts[:-1])


def check_readable(path: str) -> str:
    """The clean path, or a CodeError when the agent must not read it."""
    path = clean_path(path)
    if in_closed_folder(path):
        raise CodeError("The Code agent does not read inside .git, virtual environments, node_modules or hidden folders.")
    if is_secret(path):
        raise CodeError("That file can hold secrets. The Code agent does not read it.")
    return path


# Secrets written in code or config. The name says what it is; the value is a literal, not code that reads a secret.
_SECRET_NAME = r"[\w.-]*(?:pass(?:word|wd)?|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credential|auth[_-]?key|dsn)[\w.-]*"
QUOTED_SECRET = re.compile(rf"(?i)(\b{_SECRET_NAME}[\"']?\s*[:=]\s*)([\"'`])([^\"'`\s]{{6,}})\2")
PLAIN_SECRET = re.compile(rf"(?i)^(\s*(?:export\s+)?{_SECRET_NAME}\s*[:=]\s*)(?![\"'`$%{{<(\[])([^\s#]{{6,}})")
CONFIG_SUFFIXES = {".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".properties", ".txt", ""}


def mask_secrets(path: str, lines: list[str]) -> list[str]:
    """The lines with secret values replaced by [secret]. Same number of lines, so line numbers stay true."""
    from .privacy import AWS_KEY, BEARER, JWT, PRIVATE_KEY, PROVIDER_KEY

    config = PurePosixPath(path).suffix.lower() in CONFIG_SUFFIXES
    out = []
    for line in lines:
        for pattern in (JWT, BEARER, AWS_KEY, PROVIDER_KEY):
            line = pattern.sub("[secret]", line)
        line = QUOTED_SECRET.sub(lambda m: m.group(1) + m.group(2) + "[secret]" + m.group(2), line)
        if config:
            line = PLAIN_SECRET.sub(lambda m: m.group(1) + "[secret]", line)
        out.append(line)
    text = "\n".join(out)
    if PRIVATE_KEY.search(text):  # a private key block: keep the line count, hide each line of it
        inside = False
        for i, line in enumerate(out):
            if "PRIVATE KEY-----" in line and "BEGIN" in line:
                inside = True
            if inside:
                out[i] = "[secret]"
            if "PRIVATE KEY-----" in line and "END" in line:
                inside = False
    return out


def is_text(path: str) -> bool:
    p = PurePosixPath(path)
    return p.name in TEXT_NAMES or p.suffix.lower() in TEXT_SUFFIXES


def clean_path(path: str) -> str:
    raw = path.strip()
    while raw.startswith("./"):  # only a "./" prefix: a leading dot is part of a name such as .env
        raw = raw[2:]
    p = PurePosixPath(raw or ".")
    if p.is_absolute() or ".." in p.parts:
        raise CodeError("Use a path inside the service's code, for example src/app.py.")
    return str(p)


def glob_match(path: str, pattern: str) -> bool:
    """A glob that also matches deeper in the tree: "src/*.ts" matches "apps/web/src/lib/a.ts". Models write
    globs from the root, but real projects nest their code."""
    pattern = pattern.strip().lstrip("/")
    return not pattern or fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path, f"*/{pattern}")


def numbered(lines: list[str], start: int) -> str:
    return "\n".join(f"{n:>5}  {line}" for n, line in enumerate(lines, start))


class FolderCodeSource:
    """Code in folders on this computer. The demo code uses it too."""

    name = "Folder"

    def __init__(self, root: Path | None = None, folders: dict[str, str] | None = None, label: str = "Folder") -> None:
        self.root = Path(root) if root else None
        self.folders = {k: Path(v).expanduser() for k, v in (folders or {}).items()}
        self.name = label

    def services(self) -> list[str]:
        names = set(self.folders)
        if self.root and self.root.is_dir():
            names |= {p.name for p in self.root.iterdir() if p.is_dir() and not p.name.startswith(".")}
        return sorted(n for n in names if n != "*")

    def _base(self, service: str) -> Path:
        base = self.folders.get(service) or (self.root / service if self.root else None) or self.folders.get("*")
        if base is None or not base.is_dir():
            raise CodeError(f"No code for service '{service}'. Services with code: {', '.join(self.services()) or 'none'}.")
        return base.resolve()

    def _file(self, service: str, path: str) -> Path:
        base = self._base(service)
        target = (base / check_readable(path)).resolve()
        if base not in target.parents and target != base:
            raise CodeError("That path is outside the service's code.")
        rel = target.relative_to(base).as_posix() if target != base else "."
        if rel != "." and (in_closed_folder(rel) or is_secret(rel)):  # a link inside the folder that points to a secret
            raise CodeError("That file can hold secrets. The Code agent does not read it.")
        return target

    def _walk(self, service: str):
        base = self._base(service)
        for path in sorted(base.rglob("*")):
            rel = path.relative_to(base)
            if in_closed_folder(rel.as_posix()):
                continue
            if path.is_file() and not path.is_symlink() and not is_secret(str(rel)):
                yield str(rel).replace("\\", "/"), path

    def list_files(self, service: str, prefix: str = "") -> list[str]:
        prefix = clean_path(prefix) if prefix.strip() else ""
        files = [rel for rel, _ in self._walk(service) if not prefix or prefix == "." or rel.startswith(prefix)]
        return files[:MAX_FILES]

    def read_lines(self, service: str, path: str) -> list[str]:
        target = self._file(service, path)
        if not target.is_file():
            raise CodeError(f"No file {path} in {service}. Use list_code_files or search_code to find the path.")
        if target.stat().st_size > MAX_FILE_BYTES:
            raise CodeError(f"{path} is larger than {MAX_FILE_BYTES // 1000} KB. Search in it instead.")
        if not is_text(path):
            raise CodeError(f"{path} is not a text file.")
        return mask_secrets(path, target.read_text(errors="replace").splitlines())

    def search(self, service: str, query: str, path_glob: str = "") -> list[dict]:
        pattern = _pattern(query)
        out = []
        for rel, path in self._walk(service):
            if not glob_match(rel, path_glob) or not is_text(rel) or path.stat().st_size > MAX_FILE_BYTES:
                continue
            for n, line in enumerate(mask_secrets(rel, path.read_text(errors="replace").splitlines()), 1):
                if pattern.search(line):
                    out.append({"path": rel, "line": n, "text": line.strip()[:200]})
                    if len(out) >= MAX_RESULTS:
                        return out
        return out


class GitHubCodeSource:
    """Code in GitHub repositories, read with a read-only token. The default branch unless "repo@ref" is given."""

    name = "GitHub"

    def __init__(self, token: str, repos: dict[str, str], api: str = "https://api.github.com", client: httpx.Client | None = None) -> None:
        self.repos = repos
        self.api = api.rstrip("/")
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.client = client or httpx.Client(timeout=20, headers=headers)
        self._trees: dict[str, list[str]] = {}

    def services(self) -> list[str]:
        return sorted(k for k in self.repos if k != "*")

    def _repo(self, service: str) -> tuple[str, str]:
        spec = self.repos.get(service) or self.repos.get("*")
        if not spec:
            raise CodeError(f"No repository for service '{service}'. Services with code: {', '.join(self.services()) or 'none'}.")
        repo, _, ref = spec.partition("@")
        return repo, ref or "HEAD"

    def _get(self, path: str, params: dict | None = None):
        try:
            response = self.client.get(f"{self.api}{path}", params=params or {})
            if response.status_code == 404:
                raise CodeError("GitHub has no such file or repository (or the token cannot read it).")
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            raise CodeError(f"GitHub did not answer ({type(exc).__name__}).") from exc

    def list_files(self, service: str, prefix: str = "") -> list[str]:
        repo, ref = self._repo(service)
        if repo not in self._trees:
            tree = self._get(f"/repos/{repo}/git/trees/{ref}", {"recursive": "1"})
            self._trees[repo] = [i["path"] for i in tree.get("tree", []) if i.get("type") == "blob" and not is_secret(i["path"])
                                 and not in_closed_folder(i["path"])]
        prefix = clean_path(prefix) if prefix.strip() and prefix.strip() != "." else ""
        return [p for p in self._trees[repo] if not prefix or p.startswith(prefix)][:MAX_FILES]

    def read_lines(self, service: str, path: str) -> list[str]:
        path = check_readable(path)
        repo, ref = self._repo(service)
        data = self._get(f"/repos/{repo}/contents/{path}", {"ref": ref} if ref != "HEAD" else {})
        if isinstance(data, list) or data.get("type") != "file":
            raise CodeError(f"{path} is a folder. Use list_code_files.")
        if data.get("size", 0) > MAX_FILE_BYTES:
            raise CodeError(f"{path} is larger than {MAX_FILE_BYTES // 1000} KB. Search in it instead.")
        return mask_secrets(path, base64.b64decode(data.get("content", "")).decode(errors="replace").splitlines())

    def search(self, service: str, query: str, path_glob: str = "") -> list[dict]:
        """GitHub code search finds the files. The lines come from the files themselves, with exact line numbers."""
        repo, _ = self._repo(service)
        words = re.sub(r"[^\w.\-/ ]", " ", query).strip() or query
        hits = self._get("/search/code", {"q": f"{words} repo:{repo}", "per_page": 10}).get("items", [])
        pattern, out = _pattern(query), []
        for item in hits:
            path = item["path"]
            if is_secret(path) or in_closed_folder(path) or not glob_match(path, path_glob):
                continue
            try:
                lines = self.read_lines(service, path)
            except CodeError:
                continue
            out += [{"path": path, "line": n, "text": line.strip()[:200]} for n, line in enumerate(lines, 1) if pattern.search(line)]
            if len(out) >= MAX_RESULTS:
                break
        return out[:MAX_RESULTS]


def _pattern(query: str) -> re.Pattern:
    query = query.strip()
    if not query:
        raise CodeError("Give a word or a short pattern to search for.")
    try:
        return re.compile(query, re.IGNORECASE)
    except re.error:
        return re.compile(re.escape(query), re.IGNORECASE)


SYMBOL = r"(?:def|class|function|func|const|let|var|interface|type|struct|enum|public|private|static)\s+[\w<>\[\], ]*\b{name}\b"


def find_symbol(source, service: str, name: str) -> list[dict]:
    if not re.fullmatch(r"[A-Za-z_][\w]{1,80}", name):
        raise CodeError("Give one name, for example create_charge.")
    hits = source.search(service, SYMBOL.format(name=re.escape(name)))
    return hits or source.search(service, rf"\b{re.escape(name)}\s*[:=]")[:10]


def read_range(source, service: str, path: str, start: int = 1, end: int = 0) -> dict:
    lines = source.read_lines(service, path)
    start = max(1, int(start or 1))
    end = min(len(lines), int(end) if end else start + 119, start + MAX_LINES - 1)
    if start > len(lines):
        raise CodeError(f"{path} has only {len(lines)} lines.")
    return {"path": path, "lines": f"{start}-{end} of {len(lines)}", "code": numbered(lines[start - 1:end], start)}


def parse_map(text: str) -> dict[str, str]:
    out = {}
    for line in (text or "").replace(",", "\n").splitlines():
        if "=" in line:
            key, value = (part.strip() for part in line.split("=", 1))
            if key and value:
                out[key] = value
    return out
