import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from backend.app import main
from backend.app.agent.runner import AgentRunner
from backend.app.agent.store import AgentRunStore
from backend.app.database import CaseDatabase
from backend.app.permissions import needed_role
from backend.app.services.auth import LocalAuthenticator, UserError, check_password, hash_password
from backend.app.services.oidc import OidcClient, OidcConfig, SsoError
from backend.tests.test_agent import KeywordEmbedder, NoJira
from backend.tests.test_api import FakeAnalyst, FakeEmbedder, FakeGmailPublisher

GOOD = "Correct-horse-9"


class AuthenticatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "users.db"
        self.auth = LocalAuthenticator("admin", "pw", db_path=self.path)

    def tearDown(self):
        self.temp.cleanup()

    def bearer(self, session):
        return f"Bearer {session.access_token}"

    def test_password_hash_round_trip(self):
        stored = hash_password(GOOD)
        self.assertTrue(stored.startswith("scrypt$"))
        self.assertTrue(check_password(GOOD, stored))
        self.assertFalse(check_password("wrong", stored))
        self.assertFalse(check_password(GOOD, "garbage"))

    def test_env_account_is_admin_and_sessions_survive_a_restart(self):
        session = self.auth.login("admin", "pw")
        self.assertEqual(session.user.role, "admin")
        again = LocalAuthenticator("admin", "pw", db_path=self.path)
        self.assertEqual(again.authenticate(self.bearer(session)).username, "admin")
        self.assertNotIn(session.access_token, self.path.read_bytes().decode("latin-1"))  # only the hash is kept

    def test_new_env_password_ends_old_sessions(self):
        session = self.auth.login("admin", "pw")
        changed = LocalAuthenticator("admin", "new-pw", db_path=self.path)
        self.assertIsNone(changed.authenticate(self.bearer(session)))
        self.assertIsNone(changed.login("admin", "pw"))
        self.assertIsNotNone(changed.login("admin", "new-pw"))

    def test_roles_disable_and_reset_end_sessions(self):
        self.auth.create_user("ann", "Ann", "ann@acme.io", "engineer", GOOD)
        session = self.auth.login("ann", GOOD)
        self.assertEqual(self.auth.authenticate(self.bearer(session)).role, "engineer")
        self.auth.update_user("ann", role="viewer")
        self.assertIsNone(self.auth.authenticate(self.bearer(session)))
        session = self.auth.login("ann", GOOD)
        self.auth.update_user("ann", disabled=True)
        self.assertIsNone(self.auth.authenticate(self.bearer(session)))
        self.assertIsNone(self.auth.login("ann", GOOD))
        self.auth.update_user("ann", disabled=False, password="Another-pass-7")
        self.assertIsNone(self.auth.login("ann", GOOD))
        self.assertIsNotNone(self.auth.login("ann", "Another-pass-7"))

    def test_rules_for_new_users_and_the_env_account(self):
        with self.assertRaisesRegex(UserError, "10 or more"):
            self.auth.create_user("bob", "", "", "engineer", "Short-1")
        with self.assertRaisesRegex(UserError, "upper-case"):
            self.auth.create_user("bob", "", "", "engineer", "alllowercase123")
        with self.assertRaisesRegex(UserError, "role"):
            self.auth.create_user("bob", "", "", "owner", GOOD)
        with self.assertRaisesRegex(UserError, "username"):
            self.auth.create_user("b o b", "", "", "viewer", GOOD)
        self.auth.create_user("bob", "", "bob@acme.io", "viewer", GOOD)
        with self.assertRaisesRegex(UserError, "exists"):
            self.auth.create_user("bob2", "", "BOB@acme.io", "viewer", GOOD)
        for change in ({"role": "viewer"}, {"disabled": True}, {"password": GOOD}):
            with self.assertRaises(UserError):
                self.auth.update_user("admin", **change)

    def test_sso_users_need_an_allowed_domain_and_cannot_use_a_password(self):
        with self.assertRaisesRegex(UserError, "Ask an admin"):
            self.auth.login_sso("eve@evil.io", "Eve", {"acme.io"}, "viewer")
        session = self.auth.login_sso("Cara@Acme.io", "Cara", {"acme.io"}, "engineer")
        self.assertEqual((session.user.email, session.user.role, session.user.auth), ("cara@acme.io", "engineer", "sso"))
        self.assertIsNone(self.auth.login("cara@acme.io", ""))
        self.auth.create_user("dan", "Dan", "dan@partner.io", "viewer", None, auth="sso")  # invited by an admin
        self.assertEqual(self.auth.login_sso("dan@partner.io", "", set(), "viewer").user.username, "dan")
        self.auth.update_user("dan", disabled=True)
        with self.assertRaisesRegex(UserError, "disabled"):
            self.auth.login_sso("dan@partner.io", "", set(), "viewer")

    def test_sso_does_not_sign_in_a_password_account(self):
        self.auth.create_user("pat", "Pat", "pat@acme.io", "admin", GOOD)
        with self.assertRaisesRegex(UserError, "signs in with a password"):
            self.auth.login_sso("pat@acme.io", "Pat", {"acme.io"}, "viewer")

    def test_wrong_current_passwords_lock_the_account(self):
        from backend.app.services.auth import LoginLocked

        self.auth.create_user("pat", "Pat", "", "engineer", GOOD)
        for _ in range(self.auth.max_failures):
            with self.assertRaises(UserError):
                self.auth.change_own_password("pat", "guess", GOOD + "x")
        with self.assertRaises(LoginLocked):  # also the right password now waits for the lock to end
            self.auth.change_own_password("pat", GOOD, GOOD + "x")
        with self.assertRaises(LoginLocked):
            self.auth.login("pat", GOOD)

    def test_change_own_password_needs_the_current_one(self):
        self.auth.create_user("ann", "", "", "engineer", GOOD)
        with self.assertRaisesRegex(UserError, "current password"):
            self.auth.change_own_password("ann", "wrong", "Brand-new-pass-1")
        self.auth.change_own_password("ann", GOOD, "Brand-new-pass-1")
        self.assertIsNotNone(self.auth.login("ann", "Brand-new-pass-1"))


class PermissionTableTests(unittest.TestCase):
    def test_table(self):
        cases = [("GET", "/api/cases", "viewer"), ("PUT", "/api/preferences", "viewer"), ("POST", "/api/auth/logout", "viewer"),
                 ("POST", "/api/cases", "engineer"), ("POST", "/api/agent-runs/x/decision", "engineer"),
                 ("DELETE", "/api/cases/x", "engineer"), ("POST", "/api/bin/x/restore", "engineer"),
                 ("DELETE", "/api/bin/x", "admin"), ("GET", "/api/models", "admin"), ("PUT", "/api/sources/logs", "admin"),
                 ("GET", "/api/users", "admin"), ("PATCH", "/api/users/ann", "admin")]
        for method, path, role in cases:
            self.assertEqual(needed_role(method, path), role, (method, path))


class UserApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        data = Path(self.temp.name)
        self.saved = (main.settings, main.database, main.authenticator, main.agent_runs, main.agent_runner, main.jira_publisher,
                      main.analyst, main.embedder)
        main.analyst, main.embedder = FakeAnalyst(), FakeEmbedder()  # no real model calls in tests
        main.settings = replace(main.settings, data_dir=data, agent_autorun=False, agent_concurrency=0, agent_sla_watch_seconds=0)
        main.database = CaseDatabase(data / "rootiq.db")
        main.database.initialize()
        main.jira_publisher = NoJira()
        main.agent_runs = AgentRunStore(data / "rootiq.db")
        main.agent_runs.initialize()
        main.agent_runner = AgentRunner(main.settings, main.database, KeywordEmbedder(), NoJira(), FakeGmailPublisher(), main.agent_runs)
        main.authenticator = LocalAuthenticator("admin", "pw")
        self.admin = self.client_for("admin", "pw")

    def tearDown(self):
        (main.settings, main.database, main.authenticator, main.agent_runs, main.agent_runner, main.jira_publisher,
         main.analyst, main.embedder) = self.saved
        self.temp.cleanup()

    def client_for(self, username, password):
        client = TestClient(main.app)
        token = client.post("/api/auth/login", json={"username": username, "password": password}).json()["access_token"]
        client.headers.update({"Authorization": f"Bearer {token}"})
        return client

    def test_each_role_gets_only_its_actions(self):
        for name, role in (("eng", "engineer"), ("view", "viewer")):
            self.assertEqual(self.admin.post("/api/users", json={"username": name, "role": role, "password": GOOD}).status_code, 201)
        engineer, viewer = self.client_for("eng", GOOD), self.client_for("view", GOOD)
        case = {"title": "Checkout fails", "customer": "Acme", "service": "payments-api", "reported_issue": "502 on pay"}
        denied = viewer.post("/api/cases", json=case)
        self.assertEqual(denied.status_code, 403)
        self.assertIn("engineer role", denied.json()["detail"])
        self.assertEqual(viewer.get("/api/cases").status_code, 200)
        created = engineer.post("/api/cases", json=case)
        self.assertEqual(created.status_code, 200)
        self.assertEqual(engineer.get("/api/users").status_code, 403)
        self.assertEqual(engineer.get("/api/models").status_code, 403)
        self.assertEqual(engineer.delete(f"/api/cases/{created.json()['id']}").status_code, 204)
        self.assertEqual(engineer.delete(f"/api/bin/{created.json()['id']}").status_code, 403)
        self.assertEqual(self.admin.delete(f"/api/bin/{created.json()['id']}").status_code, 204)
        self.assertEqual(engineer.get("/api/auth/me").json()["role"], "engineer")

    def test_admin_cannot_lock_themself_out_and_changes_are_audited(self):
        self.assertEqual(self.admin.patch("/api/users/admin", json={"role": "viewer"}).status_code, 400)
        self.assertEqual(self.admin.patch("/api/users/admin", json={"disabled": True}).status_code, 400)
        self.admin.post("/api/users", json={"username": "ann", "role": "engineer", "password": GOOD})
        users = self.admin.patch("/api/users/ann", json={"role": "viewer"}).json()
        self.assertEqual(next(u for u in users if u["username"] == "ann")["role"], "viewer")
        self.assertNotIn("password_hash", users[0])
        actions = [e["action"] for e in self.admin.get("/api/audit").json()]
        self.assertIn("user.created", actions)
        self.assertIn("user.updated", actions)

    def test_remove_a_user(self):
        self.admin.post("/api/users", json={"username": "ann@acme.io", "email": "ann@acme.io", "role": "engineer"})
        self.admin.post("/api/users", json={"username": "bob", "role": "engineer", "password": GOOD})
        bob = self.client_for("bob", GOOD)
        self.assertEqual(bob.delete("/api/users/ann@acme.io").status_code, 403)  # admins only
        self.admin.post("/api/users", json={"username": "eve", "role": "admin", "password": GOOD})
        eve = self.client_for("eve", GOOD)
        self.assertEqual(eve.delete("/api/users/eve").status_code, 400)  # not your own account
        self.assertEqual(eve.delete("/api/users/admin").status_code, 400)  # not the account in backend/.env
        self.assertEqual(self.admin.delete("/api/users/nobody").status_code, 400)
        users = self.admin.delete("/api/users/bob").json()
        self.assertNotIn("bob", [u["username"] for u in users])
        self.assertEqual(bob.get("/api/cases").status_code, 401)  # the sessions end at once
        self.assertIn("user.removed", [e["action"] for e in self.admin.get("/api/audit").json()])

    def test_large_requests_and_long_fields_are_refused(self):
        big = {"title": "x", "customer": "Acme", "service": "web", "reported_issue": "y" * 2_100_000}
        self.assertEqual(self.admin.post("/api/cases", json=big).status_code, 413)
        long_logs = {"title": "x", "customer": "Acme", "service": "web", "reported_issue": "y", "logs": "z" * 200_001}
        self.assertEqual(self.admin.post("/api/cases", json=long_logs).status_code, 422)

    def test_live_channel_needs_a_session_an_allowed_website_and_a_real_case(self):
        from starlette.websockets import WebSocketDisconnect

        case = self.admin.post("/api/cases", json={"title": "Login fails", "customer": "Acme", "service": "auth",
                                                   "reported_issue": "Codes do not arrive."}).json()
        token = self.admin.headers["Authorization"].removeprefix("Bearer ")
        self.admin.cookies.set("rootiq_session", token)
        with self.admin.websocket_connect(f"/api/cases/{case['id']}/ws", headers={"origin": "http://127.0.0.1:5174"}):
            pass  # accepted
        for path, headers in ((f"/api/cases/{case['id']}/ws", {"origin": "https://evil.example"}),
                              ("/api/cases/no-such-case/ws", {})):
            with self.subTest(path=path, headers=headers), self.assertRaises(WebSocketDisconnect):
                with self.admin.websocket_connect(path, headers=headers) as ws:
                    ws.receive_text()
        self.admin.cookies.clear()
        with self.assertRaises(WebSocketDisconnect):
            with self.admin.websocket_connect(f"/api/cases/{case['id']}/ws") as ws:
                ws.receive_text()

    def test_sso_is_off_without_settings(self):
        saved, main.oidc = main.oidc, OidcClient(OidcConfig("", "", "", ""))  # not the settings in backend/.env
        try:
            self.assertEqual(TestClient(main.app).get("/api/auth/config").json()["sso"], False)
            self.assertEqual(TestClient(main.app).get("/api/auth/oidc/start", follow_redirects=False).status_code, 404)
        finally:
            main.oidc = saved


class OidcTests(unittest.TestCase):
    ISSUER = "https://id.example"

    def setUp(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key(), as_dict=True) | {"kid": "k1", "alg": "RS256"}
        self.claims = {}
        self.signer = lambda claims: jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "k1"})

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("openid-configuration"):
                return httpx.Response(200, json={"issuer": self.ISSUER, "authorization_endpoint": f"{self.ISSUER}/auth",
                                                 "token_endpoint": f"{self.ISSUER}/token", "jwks_uri": f"{self.ISSUER}/keys"})
            if request.url.path.endswith("/keys"):
                return httpx.Response(200, json={"keys": [jwk]})
            if request.url.path.endswith("/token"):
                form = parse_qs(request.content.decode())
                assert form["code_verifier"][0] and form["code"][0] == "the-code"
                return httpx.Response(200, json={"id_token": self.token})
            return httpx.Response(404)
        self.oidc = OidcClient(OidcConfig(self.ISSUER, "client-1", "secret", "http://app/cb"),
                               client=httpx.Client(transport=httpx.MockTransport(handler)))

    def begin(self, **overrides):
        params = parse_qs(urlparse(self.oidc.start()).query)
        self.assertEqual(params["code_challenge_method"], ["S256"])
        now = int(time.time())
        claims = {"iss": self.ISSUER, "aud": "client-1", "iat": now, "exp": now + 300, "nonce": params["nonce"][0],
                  "email": "ann@acme.io", "email_verified": True, "name": "Ann"} | overrides
        self.token = self.signer(claims)
        return params["state"][0]

    def test_good_sign_in(self):
        self.assertEqual(self.oidc.finish(self.begin(), "the-code"), ("ann@acme.io", "Ann"))

    def test_state_is_used_once(self):
        state = self.begin()
        self.oidc.finish(state, "the-code")
        with self.assertRaisesRegex(SsoError, "took too long"):
            self.oidc.finish(state, "the-code")

    def test_bad_tokens_are_refused(self):
        for overrides, message in (({"nonce": "other"}, "does not match"), ({"aud": "other-app"}, "not valid"),
                                   ({"iss": "https://evil"}, "not valid"), ({"exp": int(time.time()) - 3600}, "not valid"),
                                   ({"email_verified": False}, "verified email")):
            with self.subTest(overrides=overrides), self.assertRaisesRegex(SsoError, message):
                self.oidc.finish(self.begin(**overrides), "the-code")

    def test_microsoft_common_issuer_and_personal_accounts(self):
        tenant = "9188040d-6c67-4c5b-b112-36a304b66dad"
        self.ISSUER = "https://login.microsoftonline.com/{tenantid}/v2.0"  # what Microsoft's common discovery says
        real = f"https://login.microsoftonline.com/{tenant}/v2.0"
        personal = {"iss": real, "tid": tenant, "email": None, "email_verified": None, "preferred_username": "ann@outlook.com"}
        self.assertEqual(self.oidc.finish(self.begin(**personal), "the-code"), ("ann@outlook.com", "Ann"))
        work = {"iss": real.replace(tenant, "1111"), "tid": "1111", "email": None, "email_verified": None, "preferred_username": "ann@acme.io"}
        with self.assertRaisesRegex(SsoError, "verified email"):  # a work tenant's preferred_username is not an email
            self.oidc.finish(self.begin(**work), "the-code")
        with self.assertRaisesRegex(SsoError, "not valid"):  # the issuer must match the token's own tenant
            self.oidc.finish(self.begin(iss=real, tid="2222"), "the-code")

    def test_token_signed_with_a_shared_secret_is_refused(self):
        state = self.begin()
        public_pem = self.key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        claims = jwt.decode(self.token, options={"verify_signature": False})
        try:
            self.token = jwt.encode(claims, public_pem, algorithm="HS256", headers={"kid": "k1"})
        except jwt.InvalidKeyError:
            return  # PyJWT already refuses to make such a token
        with self.assertRaisesRegex(SsoError, "not valid"):
            self.oidc.finish(state, "the-code")


if __name__ == "__main__":
    unittest.main()
