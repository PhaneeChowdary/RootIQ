"""API for the data sources on the Settings page: see, test and save the sources of logs, metrics, changes, code and
the service database.

Rules:
- The API never sends a secret back. It says only if a secret is saved.
- A secret field that comes back empty keeps the saved secret. The admin does not have to type it again.
- The app tests a source with one small read before it saves it. The app does not save a source that fails the test.
- The audit log records each change, without secrets.
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import sources


class SourceBody(BaseModel):
    type: str
    config: dict[str, str] = Field(default_factory=dict)


def build_router(get_settings, get_database) -> APIRouter:
    router = APIRouter(prefix="/api/sources")

    def store():
        return sources.source_store(get_settings())

    def known(kind: str, type_: str) -> None:
        if kind not in sources.FIELDS or type_ not in sources.FIELDS[kind]:
            raise HTTPException(status_code=404, detail="Unknown source.")

    def merged(kind: str, body: SourceBody) -> dict:
        """The settings to use: the new values, plus the saved secrets that the page did not send again."""
        known(kind, body.type)
        allowed = {name for name, _, _ in sources.FIELDS[kind][body.type]}
        config = {k: v.strip() for k, v in body.config.items() if k in allowed}
        saved_type, saved = store().get(kind)
        if saved_type == body.type:
            for name in sources.secret_fields(kind, body.type):
                if not config.get(name) and saved.get(name):
                    config[name] = saved[name]
        return config

    def test(kind: str, type_: str, config: dict) -> str:
        try:
            return sources.check(kind, sources.build(kind, type_, config, get_settings()))
        except sources.SourceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("")
    def get_sources() -> dict:
        out = {}
        for kind in sources.KINDS:
            type_, config = store().get(kind)
            secrets = sources.secret_fields(kind, type_)
            out[kind] = {
                "type": type_,
                "config": {k: ("" if k in secrets else v) for k, v in config.items()},
                "saved_secrets": sorted(k for k in secrets if config.get(k)),
                "types": {t: [{"name": n, "label": label, "secret": s} for n, label, s in fields]
                          for t, fields in sources.FIELDS[kind].items()},
            }
        return out

    @router.post("/{kind}/test")
    def test_source(kind: str, body: SourceBody) -> dict:
        return {"ok": True, "detail": test(kind, body.type, merged(kind, body))}

    @router.put("/{kind}")
    def save_source(kind: str, body: SourceBody, request: Request) -> dict:
        config = merged(kind, body)
        detail = test(kind, body.type, config)
        store().save_kind(kind, body.type, config)
        get_database().record_audit(request.state.user.username, "sources.saved", "source", kind,
                                    f"{kind.capitalize()} source set to {body.type}.")
        return {"ok": True, "detail": detail, **get_sources()}

    return router
