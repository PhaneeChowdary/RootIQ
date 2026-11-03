import base64
import json
from email.message import EmailMessage
from urllib import error, parse, request


class GmailDraftError(RuntimeError):
    pass


class GmailDraftPublisher:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token

    @property
    def configured(self) -> bool:
        return all((self.client_id, self.client_secret, self.refresh_token))

    def create_draft(self, subject: str, body: str) -> str:
        if not self.configured:
            raise GmailDraftError("Gmail not configured.")
        token = self._refresh_token()
        msg = EmailMessage()
        msg["Subject"] = subject
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        req = request.Request(
            "https://gmail.googleapis.com/gmail/v1/users/me/drafts",
            data=json.dumps({"message": {"raw": raw}}).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())["id"]
        except error.HTTPError as exc:
            raise GmailDraftError(f"Gmail rejected draft ({exc.code}).") from exc
        except (error.URLError, TimeoutError) as exc:
            raise GmailDraftError("Cannot reach Gmail.") from exc
        except KeyError as exc:
            raise GmailDraftError("Unexpected Gmail response.") from exc

    def _refresh_token(self) -> str:
        body = parse.urlencode({
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
            "grant_type": "refresh_token",
        }).encode()
        req = request.Request(
            "https://oauth2.googleapis.com/token", data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST",
        )
        try:
            with request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())["access_token"]
        except error.HTTPError as exc:
            raise GmailDraftError(f"OAuth token refresh failed ({exc.code}).") from exc
        except (error.URLError, TimeoutError, KeyError) as exc:
            raise GmailDraftError("Cannot refresh Gmail credentials.") from exc
