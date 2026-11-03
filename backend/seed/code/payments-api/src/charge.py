"""Checkout and renewal charges."""
import logging
import time

from .gateway.client import GatewayUnavailable, create_charge

log = logging.getLogger("payments")


def charge(path: str, amount_cents: int, token: str, idempotency_key: str) -> dict:
    started = time.monotonic()
    try:
        result = create_charge(amount_cents, token, idempotency_key)
    except GatewayUnavailable as exc:
        latency = round((time.monotonic() - started) * 1000)
        log.error("[charge] path=%s charge failed status=%s upstream=gateway latency_ms=%s", path, exc.status, latency)
        raise
    log.info("[charge] path=%s charge ok amount=%.2f idempotency_key=%s", path, amount_cents / 100, idempotency_key)
    return result
