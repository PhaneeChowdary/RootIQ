"""Who can do what. The API checks each request against this table before it runs the request.

- viewer:   each GET request except Settings, and the own sign-out, password and preferences.
- engineer: also each other request, except the admin requests below.
- admin:    everything.

Admin requests: Settings for models, data sources, automation, users and security, and the delete of a case forever.
"""

from .services.auth import role_at_least

ADMIN_PREFIXES = ("/api/models", "/api/sources", "/api/users", "/api/security", "/api/automation")
ADMIN_ONLY = {("DELETE", "/api/bin/")}
VIEWER_WRITES = {("POST", "/api/auth/logout"), ("PUT", "/api/preferences"), ("PUT", "/api/auth/password")}


def needed_role(method: str, path: str) -> str:
    method = method.upper()
    if path.startswith(ADMIN_PREFIXES) or any(method == m and path.startswith(p) for m, p in ADMIN_ONLY):
        return "admin"
    if method in ("GET", "HEAD") or (method, path) in VIEWER_WRITES:
        return "viewer"
    return "engineer"


def allowed(role: str, method: str, path: str) -> bool:
    return role_at_least(role, needed_role(method, path))
