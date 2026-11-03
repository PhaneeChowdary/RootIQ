"""Single sign-on with OpenID Connect: Google, Microsoft Entra ID, Okta, Auth0, Keycloak and others.

The flow is the authorization code flow with PKCE:
1. The sign-in page sends the browser to /api/auth/oidc/start.
2. The app sends the browser to the identity provider, with a random state, nonce and PKCE code challenge.
3. The provider sends the browser back to /api/auth/oidc/callback with a code.
4. The app changes the code for an ID token, and checks the token: the signature with the keys of the provider, the
   issuer, the audience, the expiry, the nonce and a verified email.
5. The app finds the user by email. It adds a new user only if the email domain is allowed. See auth.login_sso.

Settings in backend/.env: ROOTIQ_OIDC_ISSUER, _CLIENT_ID, _CLIENT_SECRET and _REDIRECT_URL. Optional: _NAME,
_ALLOWED_DOMAINS as a comma list, and _DEFAULT_ROLE, viewer by default. Without an issuer, single sign-on is off.
"""

import base64
import hashlib
import secrets
import threading
import time
from dataclasses import dataclass, field

import httpx
import jwt

PENDING_SECONDS = 600
# Microsoft: the issuer of personal accounts. Its preferred_username is the email of the account.
# In work tenants an admin can change preferred_username, so the app never uses it as an email there.
MICROSOFT_CONSUMER_TENANT = "9188040d-6c67-4c5b-b112-36a304b66dad"
ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"]  # never "none" or HS*


class SsoError(RuntimeError):
    """Sign-in with the provider failed. The message is safe to show on the sign-in page."""


@dataclass
class OidcConfig:
    issuer: str
    client_id: str
    client_secret: str
    redirect_url: str
    name: str = "SSO"
    allowed_domains: set[str] = field(default_factory=set)
    default_role: str = "viewer"

    @property
    def enabled(self) -> bool:
        return bool(self.issuer and self.client_id and self.redirect_url)


class OidcClient:
    def __init__(self, config: OidcConfig, client: httpx.Client | None = None) -> None:
        self.config = config
        self.http = client or httpx.Client(timeout=15)
        self._discovery: dict | None = None
        self._pending: dict[str, tuple[str, str, float]] = {}  # state ->. Nonce, code_verifier, expires.
        self._lock = threading.Lock()

    def discovery(self) -> dict:
        if self._discovery is None:
            url = self.config.issuer.rstrip("/") + "/.well-known/openid-configuration"
            try:
                response = self.http.get(url)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise SsoError("Cannot reach the identity provider. Try again later.") from exc
            self._discovery = response.json()
        return self._discovery

    def start(self) -> str:
        """The address of the provider's sign-in page."""
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        with self._lock:
            now = time.time()
            self._pending = {k: v for k, v in self._pending.items() if v[2] > now}
            self._pending[state] = (nonce, verifier, now + PENDING_SECONDS)
        params = {"response_type": "code", "client_id": self.config.client_id, "redirect_uri": self.config.redirect_url,
                  "scope": "openid email profile", "state": state, "nonce": nonce,
                  "code_challenge": challenge, "code_challenge_method": "S256"}
        return str(httpx.URL(self.discovery()["authorization_endpoint"], params=params))

    def finish(self, state: str, code: str) -> tuple[str, str]:
        """Check the answer of the provider. Returns the email and the name."""
        with self._lock:
            pending = self._pending.pop(state, None)
        if pending is None or pending[2] < time.time():
            raise SsoError("The sign-in took too long or was started in another window. Try again.")
        nonce, verifier, _ = pending
        meta = self.discovery()
        try:
            response = self.http.post(meta["token_endpoint"], data={
                "grant_type": "authorization_code", "code": code, "redirect_uri": self.config.redirect_url,
                "client_id": self.config.client_id, "client_secret": self.config.client_secret, "code_verifier": verifier})
            response.raise_for_status()
            id_token = response.json()["id_token"]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise SsoError("The identity provider did not accept the sign-in.") from exc
        claims = self._verify(id_token, meta)
        if claims.get("nonce") != nonce:
            raise SsoError("The sign-in answer does not match this sign-in. Try again.")
        email = claims.get("email", "")
        if not email and claims.get("tid") == MICROSOFT_CONSUMER_TENANT and "@" in claims.get("preferred_username", ""):
            email = claims["preferred_username"]
        if not email or claims.get("email_verified") is False:
            raise SsoError("The identity provider did not give a verified email.")
        return email, claims.get("name", "")

    def _verify(self, id_token: str, meta: dict) -> dict:
        try:
            header = jwt.get_unverified_header(id_token)
            keys = self.http.get(meta["jwks_uri"]).json()["keys"]
            key = next((k for k in keys if k.get("kid") == header.get("kid")), None) or (keys[0] if len(keys) == 1 else None)
            if key is None:
                raise SsoError("The identity provider signed the sign-in with an unknown key.")
            issuer = meta.get("issuer", self.config.issuer)
            if "{tenantid}" in issuer:
                # Microsoft's common, organizations and consumers addresses: the issuer names the tenant of the token.
                # The tenant comes from the token; jwt.decode then checks the signature before the issuer.
                tid = jwt.decode(id_token, options={"verify_signature": False}).get("tid", "")
                if not tid:
                    raise SsoError("The sign-in token has no tenant.")
                issuer = issuer.replace("{tenantid}", tid)
            return jwt.decode(id_token, jwt.PyJWK(key).key, algorithms=ALGORITHMS,
                              audience=self.config.client_id, issuer=issuer, leeway=60)
        except jwt.PyJWTError as exc:
            raise SsoError(f"The sign-in token is not valid ({type(exc).__name__}).") from exc
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise SsoError("Cannot read the keys of the identity provider.") from exc
