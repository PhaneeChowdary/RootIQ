"""Client for the card gateway at pay.gateway.example.

The gateway's TLS certificate is issued by the company's private root CA. The process must trust that CA:
the image must have certs/corporate-root-ca.crt in the CA bundle that CA_BUNDLE points to.
"""
import json
import logging
import os
import ssl
import urllib.request

log = logging.getLogger("payments")

GATEWAY_HOST = os.environ.get("GATEWAY_HOST", "pay.gateway.example")
CA_BUNDLE = os.environ.get("CA_BUNDLE", "/etc/ssl/certs/ca-certificates.crt")
TIMEOUT_SECONDS = 5


class GatewayUnavailable(Exception):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status


def _tls_context() -> ssl.SSLContext:
    # Trusts only the CAs in CA_BUNDLE. Without the corporate root CA in that file, every handshake fails.
    return ssl.create_default_context(cafile=CA_BUNDLE)


def create_charge(amount_cents: int, token: str, idempotency_key: str) -> dict:
    body = json.dumps({"amount": amount_cents, "token": token}).encode()
    request = urllib.request.Request(f"https://{GATEWAY_HOST}/v2/charges", data=body, method="POST",
                                     headers={"Idempotency-Key": idempotency_key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, context=_tls_context(), timeout=TIMEOUT_SECONDS) as response:
            return json.load(response)
    except ssl.SSLCertVerificationError as exc:
        log.error('[gateway-client] TLS handshake failed host=%s err="%s"', GATEWAY_HOST, exc.verify_message)
        raise GatewayUnavailable(502, "tls") from exc
