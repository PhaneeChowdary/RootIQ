"""API for the Models page: save API keys, list models, test a model and select the main model.

Rules:
- The API never sends a full key to the browser. It sends only the last 4 characters.
- The app tests each new key before it saves the key. A key that the provider rejects is not saved.
- The audit log records each change. It does not record keys.
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .agent import llm


class KeyBody(BaseModel):
    api_key: str = Field(min_length=8, max_length=400)


class CheckBody(BaseModel):
    model: str = Field(min_length=1, max_length=200)


class ActiveBody(BaseModel):
    provider: str
    model: str = Field(min_length=1, max_length=200)


class RoutesBody(BaseModel):
    routes: dict[str, list[str]]


def build_router(get_settings, get_database) -> APIRouter:
    """get_settings and get_database return the current objects. Tests replace them after import."""
    router = APIRouter(prefix="/api/models")

    def known(provider: str) -> None:
        if provider not in llm.PROVIDERS:
            raise HTTPException(status_code=404, detail="Unknown provider.")

    def audit(request: Request, action: str, provider: str, detail: str) -> None:
        get_database().record_audit(request.state.user.username, action, "model", provider, detail)

    def skipped(settings, tier: str) -> list[dict]:
        """The connected providers that a chain does not use now, and why. For the code level with the Cloud models
        switch off: the cloud providers that would write the patches, so the page shows why only the local model is there."""
        out = [{"provider": p, "reason": reason} for p in llm.routes(settings)[tier]
               if p != "ollama" and llm.api_key(settings, p)
               and (reason := llm.held_back(settings, p, llm.provider_model(settings, p), tier))]
        if tier == "code" and not llm.code_cloud(settings):
            first = next((p for p in [llm.PATCH_PROVIDER, *llm.CODE_CLOUD_ROUTE] if p != "ollama" and llm.api_key(settings, p)), None)
            if first:  # the provider that would write the patch
                out.append({"provider": first, "reason": "Cloud models is off for the code source"})
        return out

    def overview() -> dict:
        settings, store = get_settings(), llm.model_store(get_settings())
        data = store.load()
        provider, model = llm.active_model(settings)
        paused = llm.pause_reason(provider, model)
        rows = []
        for pid, info in llm.PROVIDERS.items():
            saved = data.get("keys", {}).get(pid, "")
            key = llm.api_key(settings, pid)
            rows.append({
                "id": pid, "name": info["name"], "maker": info["maker"], "keys_url": info["keys_url"],
                "usage_url": info["usage_url"],
                "connected": bool(key), "key_hint": f"…{key[-4:]}" if key else "",
                "key_source": "page" if saved else ("env" if key else ""),
                "models": data.get("models", {}).get(pid, []),
                "selected_model": llm.provider_model(settings, pid),
                "tokens_today": llm.tokens_today(llm.provider_model(settings, pid)) if key else 0,
                "daily_budget": llm.daily_budget(settings, pid),
                "paused": (llm.pause_reason(pid, llm.provider_model(settings, pid)) or (0, ""))[1] if key else "",
            })
        return {
            "active": {"provider": provider, "model": model, "label": llm.model_label(settings),
                       "paused": f"{paused[1]} (resumes {_clock(paused[0])})" if paused else ""},
            "backup": {"provider": "ollama", "model": settings.agent_model, "tokens_today": llm.tokens_today(settings.agent_model)},
            "providers": rows,
            "routing": {
                "custom": bool(data.get("routes")),
                "local_only": llm.local_only(settings) and not data.get("routes"),
                "tiers": [{"tier": tier, "about": llm.TIER_INFO[tier],
                           "agents": sorted(a for a, t in llm.AGENT_TIERS.items() if t == tier and a != "code_patch"),
                           "chain": llm.routes(settings)[tier],
                           "models": [{"provider": p, "model": m, "paused": bool(p != "ollama" and llm.pause_reason(p, m))}
                                      for p, m in llm.chain_for(settings, tier)],
                           "skipped": skipped(settings, tier)}
                          for tier in llm.TIERS],
                "budgets": [{"provider": p, "model": llm.provider_model(settings, p), "budget": llm.daily_budget(settings, p),
                             "used": llm.tokens_today(llm.provider_model(settings, p))}
                            for p in llm.PROVIDERS if llm.api_key(settings, p) and llm.daily_budget(settings, p)],
            },
        }

    @router.get("")
    def get_models() -> dict:
        return overview()

    @router.put("/{provider}/key")
    def save_key(provider: str, body: KeyBody, request: Request) -> dict:
        store = llm.model_store(get_settings())
        known(provider)
        key = body.api_key.strip()
        try:
            names = llm.list_models(provider, key)
        except llm.ProviderError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        def change(data: dict) -> None:
            data.setdefault("keys", {})[provider] = key
            data.setdefault("models", {})[provider] = names
            if names and not data.get("selected", {}).get(provider):
                data.setdefault("selected", {})[provider] = names[0]

        store.update(change)
        audit(request, "models.key_saved", provider, f"{llm.PROVIDERS[provider]['maker']} key saved (…{key[-4:]}).")
        return overview()

    @router.delete("/{provider}/key")
    def remove_key(provider: str, request: Request) -> dict:
        settings = get_settings()
        store = llm.model_store(get_settings())
        known(provider)

        def change(data: dict) -> None:
            data.get("keys", {}).pop(provider, None)
            data.get("models", {}).pop(provider, None)
            if data.get("active", {}).get("provider") == provider:
                data["active"] = {"provider": "ollama", "model": settings.agent_model}

        store.update(change)
        audit(request, "models.key_removed", provider, f"{llm.PROVIDERS[provider]['maker']} key removed.")
        return overview()

    @router.post("/{provider}/refresh")
    def refresh_models(provider: str) -> dict:
        settings = get_settings()
        store = llm.model_store(get_settings())
        known(provider)
        key = llm.api_key(settings, provider)
        if not key:
            raise HTTPException(status_code=400, detail="Add a key first.")
        try:
            names = llm.list_models(provider, key)
        except llm.ProviderError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        store.update(lambda data: data.setdefault("models", {}).__setitem__(provider, names))
        return overview()

    @router.put("/{provider}/selected")
    def select_model(provider: str, body: CheckBody, request: Request) -> dict:
        """The model that this provider uses, from its list. Saved at once: the agents use it from the next call."""
        settings = get_settings()
        known(provider)
        if not llm.api_key(settings, provider):
            raise HTTPException(status_code=400, detail="Add a key first.")
        listed = llm.model_store(settings).load().get("models", {}).get(provider, [])
        if listed and body.model not in listed:
            raise HTTPException(status_code=422, detail=f"{body.model} is not in the model list of this key.")
        llm.model_store(settings).update(lambda data: data.setdefault("selected", {}).__setitem__(provider, body.model))
        audit(request, "models.selected", provider, f"{llm.PROVIDERS[provider]['maker']} model set to {body.model}.")
        return overview()

    @router.post("/{provider}/check")
    def check(provider: str, body: CheckBody) -> dict:
        settings = get_settings()
        store = llm.model_store(get_settings())
        known(provider)
        key = llm.api_key(settings, provider)
        if not key:
            raise HTTPException(status_code=400, detail="Add a key first.")
        store.update(lambda data: data.setdefault("selected", {}).__setitem__(provider, body.model))
        checks = llm.check_model(provider, body.model, key)
        return {"model": body.model, "checks": checks, "ok": all(c["ok"] for c in checks)}

    @router.put("/routes")
    def save_routes(body: RoutesBody, request: Request) -> dict:
        """Save the chain of each task level. Each chain: 1 to 4 different providers, from those with a key."""
        settings = get_settings()
        clean: dict[str, list[str]] = {}
        for tier in llm.TIERS:
            chain = body.routes.get(tier) or []
            if not chain or len(chain) > 4 or len(set(chain)) != len(chain):
                raise HTTPException(status_code=422, detail=f"The {tier} chain needs 1 to 4 different models.")
            for provider in chain:
                if provider != "ollama":
                    known(provider)
                    if not llm.api_key(settings, provider):
                        raise HTTPException(status_code=400, detail=f"Add a key for {llm.PROVIDERS[provider]['name']} first.")
            clean[tier] = chain
        llm.model_store(settings).update(lambda data: data.__setitem__("routes", clean))
        audit(request, "models.routes_saved", "routing", "; ".join(f"{t}: {' > '.join(c)}" for t, c in clean.items()))
        return overview()

    @router.delete("/routes")
    def reset_routes(request: Request) -> dict:
        llm.model_store(get_settings()).update(lambda data: data.pop("routes", None))
        audit(request, "models.routes_reset", "routing", "Model chains set back to the defaults.")
        return overview()

    @router.put("/active")
    def set_active(body: ActiveBody, request: Request) -> dict:
        settings = get_settings()
        store = llm.model_store(get_settings())
        if body.provider != "ollama":
            known(body.provider)
            if not llm.api_key(settings, body.provider):
                raise HTTPException(status_code=400, detail="Add a key for this provider first.")
        model = settings.agent_model if body.provider == "ollama" else body.model

        def change(data: dict) -> None:
            data["active"] = {"provider": body.provider, "model": model}
            if body.provider != "ollama":
                data.setdefault("selected", {})[body.provider] = model

        store.update(change)
        audit(request, "models.activated", body.provider, f"Main model set to {model}.")
        return overview()

    return router


def _clock(timestamp: float) -> str:
    from datetime import datetime

    from .config import Settings, zone

    return datetime.fromtimestamp(timestamp, tz=zone(Settings().time_zone)).strftime("%H:%M %Z")
