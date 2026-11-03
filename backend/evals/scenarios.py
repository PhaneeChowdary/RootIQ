"""Evaluation scenarios: incidents with known root causes, the logs that show them, and a scoring rubric.

Each scenario starts from the 10 standard resolved incidents in seed/incidents.json, the shared history. Then it adds
its own case and its own service logs. Five kinds:

  recurrence  a past incident occurs again; the history and the logs find it quickly
  lookalike   the history holds a similar incident with a DIFFERENT cause; a copy of it is wrong
  context     the ERROR lines alone mislead; the cause is in the INFO or WARN lines near them
  no_evidence nothing supports a cause; the right answer is "unknown, need more information"
  attached    the reporter attached the logs that decide it; even a single prompt finds it

The scoring is deterministic. `cause` is a list of term groups. The likely cause is correct when each group has a
match, as a substring that ignores case, and partial when some groups match. `lookalike` terms in an answer that is
not correct show that the system copied the wrong history.
"""
from dataclasses import dataclass, field

UNKNOWN_TERMS = ["unknown", "insufficient", "not enough", "no evidence", "unclear", "cannot be determined",
                 "could not determine", "unable to determine", "not determined", "no logs", "unconfirmed",
                 "more information", "not identified", "undetermined"]


@dataclass
class Scenario:
    id: str
    kind: str
    title: str
    service: str
    reported_issue: str
    cause: list[list[str]]
    logs: dict[str, str] = field(default_factory=dict)
    lookalike: list[str] = field(default_factory=list)
    attached_logs: str = ""
    customer: str = "Eval Customer"
    severity: str = "high"


def log(*lines: str) -> str:
    return "\n".join(f"2026-10-02T{line}" for line in lines) + "\n"


SCENARIOS = [
    # recurrence
    Scenario(
        "rec-otp-429", "recurrence", "Login codes missing for UK users", "auth-service",
        "Since 09:00 many UK users never receive the SMS login code. Other regions look fine.",
        cause=[["429", "rate limit", "rate-limit", "too many requests", "rate-limited", "throttl"],
               ["shared", "campaign", "marketing", "pool"]],
        logs={"auth-service": log(
            "08:58:00Z INFO [api] GET /health 200 2ms",
            "09:00:00Z INFO [campaign-dispatcher] job=winter-promo-uk started pool=shared-uk recipients=250000",
            "09:01:10Z INFO [otp-sender] sent otp region=us-east pool=otp-us status=201",
            "09:02:44Z WARN [otp-sender] provider=twilio pool=shared-uk status=429 Too Many Requests region=uk",
            "09:02:44Z ERROR [otp-sender] otp send failed user=u_771 region=uk, not retrying",
            "09:05:12Z WARN [otp-sender] provider=twilio pool=shared-uk status=429 Too Many Requests region=uk",
            "09:05:12Z ERROR [otp-sender] otp send failed user=u_902 region=uk, not retrying",
            "09:06:00Z INFO [otp-sender] sent otp region=us-east pool=otp-us status=201",
        )},
    ),
    Scenario(
        "rec-clock-drift", "recurrence", "Admins signed out every few minutes", "auth-service",
        "Admin portal users are signed out every 2 to 4 minutes since this morning.",
        cause=[["clock", "drift", "ntp", "skew", "time sync"], ["auth-05"]],
        logs={"auth-service": log(
            "07:00:00Z INFO [api] GET /health 200 3ms node=auth-02",
            "07:10:21Z WARN [jwt] token rejected reason=expired iat_in_future issuer=auth-05",
            "07:10:22Z INFO [login] password ok user=admin_12 node=auth-05",
            "07:12:40Z WARN [jwt] token rejected reason=expired iat_in_future issuer=auth-05",
            "07:15:00Z WARN [ntp] node=auth-05 offset=412s sync=failed",
            "07:16:03Z WARN [jwt] token rejected reason=expired iat_in_future issuer=auth-05",
        )},
    ),
    Scenario(
        "rec-cdn-chunks", "recurrence", "Reports page blank after release", "web-dashboard",
        "After today's release the Reports page shows empty panels. A hard refresh fixes it for some users.",
        cause=[["cdn", "cache", "cached", "stale"], ["chunk", "index.html", "purge", "javascript", "js"]],
        logs={"web-dashboard": log(
            "10:00:01Z INFO [deploy] release=2026.10.02-3 started",
            "10:01:30Z INFO [deploy] release=2026.10.02-3 complete removed_chunks=22 cdn_purge=skipped",
            "10:03:02Z INFO [cdn] GET /index.html cache=HIT age=5400s",
            "10:03:03Z WARN [cdn] GET /static/js/chunk-91.aa1.js 404 origin=dash-static",
            "10:03:03Z ERROR [browser-report] ChunkLoadError: Loading chunk 91 failed page=reports",
            "10:05:40Z INFO [cdn] GET /index.html cache=MISS age=0 client=incognito",
        )},
    ),
    Scenario(
        "rec-codec", "recurrence", "Android voice notes fail to upload", "file-upload",
        "Voice notes recorded on Android fail with 'unsupported format'. iPhone recordings work.",
        cause=[["opus", "codec", "decoder"], ["ffmpeg", "transcod", "image"]],
        logs={"file-upload": log(
            "06:30:00Z INFO [deploy] transcoder image=ffmpeg-slim:7.1 rolled out",
            "06:41:12Z INFO [transcode] m4a -> wav ok duration_s=40",
            "06:44:50Z ERROR [transcode] ffmpeg: Decoder (codec opus) not found input=.ogg",
            "06:45:02Z ERROR [upload] conversion failed format=ogg/opus user=u_31",
            "06:52:19Z ERROR [transcode] ffmpeg: Decoder (codec opus) not found input=.ogg",
        )},
    ),
    Scenario(
        "rec-dkim", "recurrence", "Signup confirmation emails go to spam", "notifications",
        "New users say the signup confirmation email lands in spam since yesterday.",
        cause=[["dkim"], ["dns", "record", "rotat", "selector", "key"]],
        logs={"notifications": log(
            "08:00:00Z INFO [dkim] key rotated selector=s2 domain=mail.example.com dns_published=false",
            "08:05:13Z WARN [smtp] sent template=signup_confirm dkim=fail selector=s2",
            "08:06:40Z WARN [smtp] sent template=signup_confirm dkim=fail selector=s2",
            "08:07:00Z INFO [smtp] sent template=order_receipt dkim=pass selector=s1",
        )},
    ),
    # lookalike: similar history, different cause
    Scenario(
        "look-sms-sender-id", "lookalike", "SMS codes not arriving for Brazil", "auth-service",
        "Users in Brazil do not get SMS login codes since this morning. Other countries are fine.",
        cause=[["sender id", "sender registration", "unregistered", "carrier", "registration"]],
        lookalike=["429", "rate limit", "rate-limit", "too many requests", "campaign"],
        logs={"auth-service": log(
            "08:00:00Z INFO [otp-sender] sent otp region=us-east status=201",
            "08:10:12Z INFO [otp-sender] provider accepted otp region=br status=201 sid=SM01",
            "08:10:40Z ERROR [sms-callback] delivery failed sid=SM01 region=br code=30007 carrier rejected: unregistered sender id",
            "08:12:02Z INFO [otp-sender] provider accepted otp region=br status=201 sid=SM02",
            "08:12:30Z ERROR [sms-callback] delivery failed sid=SM02 region=br code=30007 carrier rejected: unregistered sender id",
            "08:13:00Z INFO [compliance] BR carriers require registered alphanumeric sender id from 2026-10-02",
        )},
    ),
    Scenario(
        "look-dash-config", "lookalike", "Dashboard widgets blank after release", "web-dashboard",
        "After the 11:00 release all widgets are blank for every user, including in incognito windows.",
        cause=[["tenant_config", "tenantconfig", "missing column", "migration", "/api/config", "schema"]],
        lookalike=["cdn", "chunk", "purge", "stale index"],
        logs={"web-dashboard": log(
            "11:00:00Z INFO [deploy] release=2026.10.02-4 complete removed_chunks=0 cdn_purge=done",
            "11:00:30Z INFO [flags] feature new-widgets enabled for all tenants",
            "11:02:10Z ERROR [api] GET /api/config 500 error=column tenant_config does not exist",
            "11:02:10Z ERROR [browser-report] TypeError: Cannot read properties of undefined (reading 'tenantConfig')",
            "11:02:15Z INFO [cdn] GET /index.html cache=MISS age=0",
            "11:03:00Z WARN [migrations] pending migration 0042_add_tenant_config not applied",
        )},
    ),
    Scenario(
        "look-webhook-dupes", "lookalike", "Some customers charged twice today", "payments-api",
        "A handful of customers see two identical charges from this morning.",
        cause=[["webhook", "event"], ["duplicate delivery", "dedupe", "redis", "processed twice", "twice"]],
        lookalike=["idempotency_key=null", "idempotency key was null", "missing idempotency", "without forwarding"],
        logs={"payments-api": log(
            "09:00:00Z INFO [charge] path=checkout charge ok idempotency_key=ck_101",
            "09:00:02Z INFO [webhook] payment.succeeded event_id=evt_88 handled",
            "09:00:03Z WARN [dedupe] redis timeout after 2000ms, skipping dedupe check",
            "09:00:03Z INFO [webhook] payment.succeeded event_id=evt_88 handled",
            "09:00:04Z ERROR [fulfilment] order ord_55 captured twice for event_id=evt_88",
            "09:05:00Z INFO [charge] path=checkout charge ok idempotency_key=ck_102",
        )},
    ),
    Scenario(
        "look-upload-413", "lookalike", "Large uploads fail immediately", "file-upload",
        "Uploads above 100 MB fail right away with an error. Smaller files work.",
        cause=[["413", "too large", "body size", "client_max_body_size", "body_size"]],
        lookalike=["presigned", "expired", "expiry"],
        logs={"file-upload": log(
            "07:00:00Z INFO [config] nginx reloaded client_max_body_size=100m (was 5g)",
            "07:10:21Z WARN [nginx] 413 Request Entity Too Large size_mb=240 path=/upload",
            "07:12:02Z WARN [nginx] 413 Request Entity Too Large size_mb=180 path=/upload",
            "07:15:30Z INFO [upload] multipart complete size_mb=60 parts=4",
        )},
    ),
    Scenario(
        "look-smtp-throttle", "lookalike", "Order emails delayed by an hour", "notifications",
        "Order confirmation emails arrive about an hour late since 08:00.",
        cause=[["421", "throttl", "rate limit", "rate-limit", "rate limited"], ["smtp", "provider", "mail"]],
        lookalike=["templaterendererror", "template", "malformed", "consumer crash"],
        logs={"notifications": log(
            "08:00:00Z INFO [consumer] queue=email depth=180 consumers=6 healthy=6",
            "08:05:10Z WARN [smtp] 421 4.7.0 rate limited by provider, deferring message",
            "08:05:11Z WARN [smtp] 421 4.7.0 rate limited by provider, deferring message",
            "08:30:00Z INFO [consumer] queue=email depth=9200 consumers=6 healthy=6 oldest_age_s=3100",
            "08:31:10Z WARN [smtp] 421 4.7.0 rate limited by provider, deferring message",
        )},
    ),
    # context: the cause is in the lines around the errors
    Scenario(
        "ctx-db-password", "context", "Customers cannot log in (server error)", "auth-service",
        "Every login attempt shows 'Something went wrong' since about 05:00.",
        cause=[["password", "credential", "secret"], ["rotat", "v14", "propagat", "not updated", "stale"]],
        logs={"auth-service": log(
            "04:59:00Z INFO [secrets] DB_PASSWORD rotated version=v14 consumers_notified=billing,reports",
            "05:00:12Z WARN [db] authentication failed for user auth_svc using secret version=v13",
            "05:00:12Z ERROR [login] 500 internal error: database unavailable",
            "05:01:40Z WARN [db] authentication failed for user auth_svc using secret version=v13",
            "05:01:40Z ERROR [login] 500 internal error: database unavailable",
        )},
    ),
    Scenario(
        "ctx-reindex-lock", "context", "Dashboards very slow every night", "web-dashboard",
        "Between 02:00 and 03:00 dashboards take over a minute to load or time out.",
        cause=[["reindex", "re-index", "index rebuild"], ["lock", "exclusive"]],
        logs={"web-dashboard": log(
            "02:00:00Z INFO [cron] nightly reindex started table=events lock=exclusive",
            "02:01:12Z ERROR [api] GET /api/widgets timeout after 60000ms",
            "02:03:40Z ERROR [api] GET /api/widgets timeout after 60000ms",
            "02:58:30Z INFO [cron] nightly reindex finished table=events",
            "03:00:10Z INFO [api] GET /api/widgets 200 88ms",
        )},
    ),
    Scenario(
        "ctx-disk-full", "context", "Uploads failing with a storage error", "file-upload",
        "Since late last night every upload fails with 'could not save file'.",
        cause=[["disk", "space", "full", "99%", "storage"], ["cleanup", "clean-up", "tmp"]],
        logs={"file-upload": log(
            "00:00:00Z INFO [cleanup] tmp cleanup job disabled by config change cleanup_enabled=false",
            "05:10:00Z WARN [disk] /var/uploads usage=99%",
            "05:12:30Z ERROR [upload] write failed path=/var/uploads/tmp/8812.part",
            "05:13:01Z ERROR [upload] write failed path=/var/uploads/tmp/8813.part",
        )},
    ),
    Scenario(
        "ctx-apns-key", "context", "iPhone push notifications stopped", "notifications",
        "No iPhone user has received a push notification since 06:00. Android is fine.",
        cause=[["apns", "apple push", "push certificate", "provider token"], ["key", "token", "rotat"]],
        logs={"notifications": log(
            "06:00:00Z INFO [apns] signing key rotated key_id=K2 (old key K1 revoked)",
            "06:00:05Z WARN [apns] config still references key_id=K1",
            "06:01:12Z ERROR [push] apns 403 InvalidProviderToken device=ios",
            "06:01:13Z INFO [push] fcm 200 delivered device=android",
            "06:02:30Z ERROR [push] apns 403 InvalidProviderToken device=ios",
        )},
    ),
    Scenario(
        "ctx-payout-balance", "context", "Refunds failing for all customers", "payments-api",
        "Support cannot issue any refund this morning; every refund fails.",
        cause=[["payout", "sweep", "settlement"], ["balance", "funds", "empty", "insufficient"]],
        logs={"payments-api": log(
            "01:58:00Z INFO [payouts] daily payout sweep moved 100% of settlement balance new_balance=0.00",
            "08:10:30Z ERROR [refund] refund failed: insufficient balance amount=49.00",
            "08:12:02Z ERROR [refund] refund failed: insufficient balance amount=19.00",
            "08:15:00Z INFO [charge] path=checkout charge ok idempotency_key=ck_301",
        )},
    ),
    # no evidence: the honest answer is "unknown"
    Scenario(
        "none-search", "no_evidence", "Search feels slow", "search-service",
        "A customer says search feels slower than last week. No error messages.",
        cause=[UNKNOWN_TERMS], severity="medium",
    ),
    Scenario(
        "none-tax", "no_evidence", "Invoice shows wrong tax amount", "billing",
        "One invoice for a German customer shows 16% VAT instead of 19%.",
        cause=[UNKNOWN_TERMS], severity="medium",
    ),
    Scenario(
        "none-auth-quiet", "no_evidence", "One customer cannot log in", "auth-service",
        "A single user says login does not work for them. No other reports.",
        cause=[UNKNOWN_TERMS], lookalike=["429", "rate limit", "clock", "drift", "twilio", "ntp"], severity="low",
        logs={"auth-service": log(
            "09:00:00Z INFO [api] GET /health 200 2ms",
            "09:05:00Z INFO [login] password ok user=u_12",
            "09:10:00Z INFO [otp-sender] sent otp region=us-east pool=otp-us status=201",
        )},
    ),
    # attached: the reporter's logs already contain the answer
    Scenario(
        "att-cors", "attached", "Dashboard API calls blocked in the browser", "web-dashboard",
        "Since the morning deploy the dashboard shows 'network error' for Globex users on app.globex.io.",
        cause=[["cors"], ["origin", "allowlist", "allow-list", "allowed"]],
        attached_logs="Access to fetch at 'https://api.example.com/widgets' from origin 'https://app.globex.io' "
                      "has been blocked by CORS policy: No 'Access-Control-Allow-Origin' header",
        logs={"web-dashboard": log(
            "07:30:00Z INFO [config] cors allowlist updated removed=app.globex.io",
            "07:41:10Z WARN [api] OPTIONS /widgets 403 origin=https://app.globex.io cors=denied",
        )},
    ),
    Scenario(
        "att-issuer-decline", "attached", "Card payments declined for one bank", "payments-api",
        "Cards from one bank are declined since this morning; other cards work.",
        cause=[["issuer", "bank"], ["declin", "do_not_honor", "do not honor"]],
        attached_logs="Payment failed: card_declined (do_not_honor) returned by issuer bin=411111",
        logs={"payments-api": log(
            "08:00:00Z INFO [charge] path=checkout charge ok bin=550000",
            "08:02:10Z WARN [charge] path=checkout declined code=do_not_honor bin=411111 source=issuer",
            "08:03:40Z WARN [charge] path=checkout declined code=do_not_honor bin=411111 source=issuer",
        )},
    ),
]
