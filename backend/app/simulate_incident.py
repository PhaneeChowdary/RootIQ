"""Write a realistic incident into the service logs, for a live demo of the Grafana alert flow.

    .venv/bin/python -m backend.app.simulate_incident payments-tls     # write the incident
    .venv/bin/python -m backend.app.simulate_incident --list           # show the scenarios
    .venv/bin/python -m backend.app.simulate_incident --reset          # put the logs back as they were

What occurs after you write an incident:
1. The number of ERROR lines in the service log goes up. /metrics shows it.
2. Prometheus reads the number. The Grafana alert rule "Service errors rising" fires after about 1 minute.
3. Grafana sends the alert to RootIQ. RootIQ opens a case and the agent team starts.
4. The agents read the new log lines and find the cause. The remediation agent proposes a fix.
5. After an engineer approves, the demo runner applies the fix to the demo log: the right fix ends the errors, and a
   wrong fix does not. The verification agent then checks the result. backend/data/demo_state.json says which fix
   cures which incident.

Before the first change, the command keeps a copy of each log in backend/data/log-backup. --reset uses that copy.
"""

import argparse
import json
import shutil
import sys
from datetime import UTC, datetime, timedelta

from .config import Settings

# Each line: seconds before now, level and text. The times end now.
SCENARIOS: dict[str, dict] = {
    "payments-tls": {
        "service": "payments-api",
        "fixed_by": {"rollback_release": {"to_version": "2026.10.02-4"}},
        "healthy": ["INFO [deploy] release=payments-api:2026.10.02-4 running ca_bundle=corporate",
                    "INFO [gateway-client] TLS handshake ok host=pay.gateway.example",
                    "INFO [charge] path=checkout charge ok amount=24.00 latency_ms=598",
                    "INFO [charge] path=renewal charge ok amount=9.00 latency_ms=611"],
        "still_failing": ['ERROR [gateway-client] TLS handshake failed host=pay.gateway.example err="certificate verify failed: unable to get local issuer certificate"',
                          "ERROR [charge] path=checkout charge failed status=502 upstream=gateway latency_ms=40",
                          'ERROR [gateway-client] TLS handshake failed host=pay.gateway.example err="certificate verify failed: unable to get local issuer certificate"'],
        "about": "A new image has an old CA bundle: TLS to the card gateway fails and checkout returns 502.",
        "lines": [
            (240, "INFO", "[deploy] release=payments-api:2026.10.03-1 started previous=2026.10.02-4 base_image=alpine:3.20"),
            (225, "INFO", "[deploy] release=payments-api:2026.10.03-1 complete ca_bundle=image-default"),
            (200, "INFO", "[charge] path=checkout charge ok amount=24.00 idempotency_key=ck_b201 latency_ms=610"),
            (170, "ERROR", '[gateway-client] TLS handshake failed host=pay.gateway.example err="certificate verify failed: unable to get local issuer certificate"'),
            (170, "ERROR", "[charge] path=checkout charge failed status=502 upstream=gateway latency_ms=38"),
            (140, "ERROR", '[gateway-client] TLS handshake failed host=pay.gateway.example err="certificate verify failed: unable to get local issuer certificate"'),
            (140, "ERROR", "[charge] path=checkout charge failed status=502 upstream=gateway latency_ms=41"),
            (95, "ERROR", '[gateway-client] TLS handshake failed host=pay.gateway.example err="certificate verify failed: unable to get local issuer certificate"'),
            (95, "ERROR", "[charge] path=renewal charge failed status=502 upstream=gateway latency_ms=36"),
            (60, "WARN", "[gateway-client] success rate over 5m=12% host=pay.gateway.example"),
            (30, "ERROR", '[gateway-client] TLS handshake failed host=pay.gateway.example err="certificate verify failed: unable to get local issuer certificate"'),
            (30, "ERROR", "[charge] path=checkout charge failed status=502 upstream=gateway latency_ms=39"),
        ],
    },
    "dashboard-db-pool": {
        "service": "web-dashboard",
        # the fix that cures it in the demo environment, see automation.DemoRunner. Other fixes leave the errors.
        "fixed_by": {"revert_config_change": {"change_id": "e7a90c3"}},
        "healthy": ["INFO [config] reload ok changed=db.pool.max_size old=2 new=40 by=rootiq",
                    "INFO [db] pool ok size=40 waiting=0", "INFO [api] GET /api/widgets 200 92ms",
                    "INFO [api] GET /api/reports 200 140ms"],
        "still_failing": ["WARN [db] pool exhausted size=2 waiting=24",
                          "ERROR [api] GET /api/widgets 500 timeout acquiring db connection after 5000ms",
                          "ERROR [api] GET /api/widgets 500 timeout acquiring db connection after 5000ms",
                          "ERROR [api] GET /api/reports 500 timeout acquiring db connection after 5000ms"],
        "about": "A config change lowers the database pool to 2 connections: the widgets API times out.",
        "lines": [
            (265, "INFO", "[deploy] config change=e7a90c3 by=deploy-bot db.pool.max_size 40 -> 2 (copied from the staging profile)"),
            (260, "INFO", "[config] reload ok changed=db.pool.max_size old=40 new=2 by=deploy-bot"),
            (220, "INFO", "[api] GET /api/widgets 200 95ms"),
            (180, "WARN", "[db] pool exhausted size=2 waiting=17"),
            (175, "ERROR", "[api] GET /api/widgets 500 timeout acquiring db connection after 5000ms"),
            (150, "ERROR", "[api] GET /api/widgets 500 timeout acquiring db connection after 5000ms"),
            (120, "WARN", "[db] pool exhausted size=2 waiting=31"),
            (115, "ERROR", "[api] GET /api/widgets 500 timeout acquiring db connection after 5000ms"),
            (80, "ERROR", "[api] GET /api/reports 500 timeout acquiring db connection after 5000ms"),
            (40, "ERROR", "[api] GET /api/widgets 500 timeout acquiring db connection after 5000ms"),
            (10, "ERROR", "[api] GET /api/widgets 500 timeout acquiring db connection after 5000ms"),
        ],
    },
    # Incidents for real projects: the logs are simulated, the code is the real code of each project. The traceback
    # lines and the log keys match the files and the names in that code, so the Code agent can follow them.
    "trialiq-null-lab": {
        "service": "trialiq",
        "fixed_by": {"revert_config_change": {"change_id": "c4f1a9e"}},
        "healthy": ["INFO [config] reload ok changed=ehr.labs.import_censored old=true new=false by=rootiq",
                    "INFO [ehr-sync] labs batch loaded rows=1803 null_values=0 source=lis-feed",
                    'INFO [uvicorn.access] "GET /api/trials/NCT06742723/estimate HTTP/1.1" 200'],
        "still_failing": ['INFO [uvicorn.access] "GET /api/trials/NCT06742723/estimate HTTP/1.1" 500',
                          "ERROR [uvicorn.error] Exception in ASGI application: TypeError: '<' not supported between instances of 'NoneType' and 'float'"],
        "about": "TrialIQ: the lab feed keeps censored eGFR results ('>60') with no value; screening the CKD trial fails with a TypeError.",
        "lines": [
            (1500, "INFO", "[config] change=c4f1a9e by=data-eng ehr.labs.import_censored false -> true (keep '>60' and 'pending' lab rows; value stored as NULL)"),
            (1490, "INFO", "[config] reload ok changed=ehr.labs.import_censored old=false new=true by=data-eng"),
            (1400, "INFO", "[ehr-sync] labs batch loaded rows=1840 null_values=37 source=lis-feed loinc=62238-1,33914-3"),
            (1100, "INFO", '[uvicorn.access] "GET /api/trials/NCT07668388/estimate HTTP/1.1" 200'),
            (900, "INFO", '[uvicorn.access] "GET /api/trials/NCT06742723/estimate HTTP/1.1" 500'),
            (900, "ERROR", "[uvicorn.error] Exception in ASGI application: GET /api/trials/NCT06742723/estimate"),
            (900, "ERROR", '[uvicorn.error] File "backend/app/main.py", line 230, in estimate'),
            (900, "ERROR", '[uvicorn.error] File "backend/app/engine.py", line 347, in screen_all'),
            (900, "ERROR", '[uvicorn.error] File "backend/app/engine.py", line 338, in criterion'),
            (900, "ERROR", '[uvicorn.error] File "backend/app/engine.py", line 161, in atom'),
            (900, "ERROR", '[uvicorn.error] File "backend/app/engine.py", line 303, in _lab'),
            (900, "ERROR", '[uvicorn.error] File "backend/app/engine.py", line 135, in in_range'),
            (900, "ERROR", "[uvicorn.error] TypeError: '<' not supported between instances of 'NoneType' and 'float'"),
            (420, "INFO", '[uvicorn.access] "POST /api/trials/NCT06742723/screen HTTP/1.1" 500'),
            (420, "ERROR", "[uvicorn.error] Exception in ASGI application: TypeError: '<' not supported between instances of 'NoneType' and 'float'"),
            (60, "INFO", '[uvicorn.access] "GET /api/trials/NCT06742723/estimate HTTP/1.1" 500'),
            (60, "ERROR", "[uvicorn.error] Exception in ASGI application: TypeError: '<' not supported between instances of 'NoneType' and 'float'"),
        ],
    },
    "triagelm-truncated-json": {
        "service": "triagelm",
        "fixed_by": {"rollback_release": {"to_version": "triage-ft:2026.10.03"}},
        "healthy": ["INFO [deploy] model=triage-ft:2026.10.03 running schema=v1",
                    "INFO [triage] ticket triaged chars=1630 json_ok=true schema_ok=true ms=2210",
                    "INFO [triage] ticket triaged chars=1420 json_ok=true schema_ok=true ms=1980"],
        "still_failing": ["WARN [triage] generation stopped at the token limit done_reason=length eval_count=256 num_predict=256 model=triage-ft",
                          "ERROR [triage] (model did not return parseable JSON) chars=1580 json_ok=false schema_ok=false"],
        "about": "TriageLM: a model update makes the JSON longer; long tickets hit num_predict=256 and the JSON is cut off.",
        "lines": [
            (1700, "INFO", "[deploy] model=triage-ft:2026.10.10 started previous=triage-ft:2026.10.03 schema=v2 (adds entities.order_ids and routing.secondary_team)"),
            (1690, "INFO", "[triage] model loaded name=triage-ft num_predict=256 temperature=0.0"),
            (1500, "INFO", "[triage] ticket triaged chars=212 json_ok=true schema_ok=true ms=860"),
            (1200, "WARN", "[triage] generation stopped at the token limit done_reason=length eval_count=256 num_predict=256 model=triage-ft"),
            (1200, "ERROR", "[triage] (model did not return parseable JSON) chars=1630 json_ok=false schema_ok=false"),
            (1000, "INFO", "[triage] ticket triaged chars=388 json_ok=true schema_ok=true ms=1010"),
            (700, "WARN", "[triage] generation stopped at the token limit done_reason=length eval_count=256 num_predict=256 model=triage-ft"),
            (700, "ERROR", "[triage] (model did not return parseable JSON) chars=2104 json_ok=false schema_ok=false"),
            (240, "WARN", "[triage] generation stopped at the token limit done_reason=length eval_count=256 num_predict=256 model=triage-ft"),
            (240, "ERROR", "[triage] (model did not return parseable JSON) chars=1475 json_ok=false schema_ok=false"),
            (200, "WARN", "[triage] generation stopped at the token limit done_reason=length eval_count=256 num_predict=256 model=triage-ft"),
            (200, "ERROR", "[triage] (model did not return parseable JSON) chars=1912 json_ok=false schema_ok=false"),
            (150, "WARN", "[triage] generation stopped at the token limit done_reason=length eval_count=256 num_predict=256 model=triage-ft"),
            (150, "ERROR", "[triage] (model did not return parseable JSON) chars=1702 json_ok=false schema_ok=false"),
            (90, "ERROR", "[triage] fallback=manual_queue tickets_last_30m=5 reason=json_ok=false"),
        ],
    },
    "eu-otp-email": {
        "service": "auth-service",
        "fixed_by": {"disable_feature_flag": {"flag": "autumn_promo_eu_sms"}},
        "healthy": ["INFO [flags] autumn_promo_eu_sms=off by=rootiq",
                    "INFO [otp-sender] otp sent provider=twilio pool=shared-eu region=eu-west latency_ms=820",
                    "INFO [login] otp verified region=eu-west"],
        "still_failing": ["WARN [otp-sender] provider=twilio pool=shared-eu status=429 Too Many Requests region=eu-west",
                          "ERROR [otp-sender] otp send failed region=eu-west, not retrying"],
        "about": "An EU SMS campaign uses the login text pool: login codes fail, and the email fallback floods the email queue.",
        "lines": [
            (300, "INFO", "[flags] autumn_promo_eu_sms=on by=marketing-ops"),
            (290, "INFO", "[campaign-dispatcher] job=autumn-promo-eu channel=sms pool=shared-eu started"),
            (240, "WARN", "[otp-sender] provider=twilio pool=shared-eu status=429 Too Many Requests region=eu-west"),
            (240, "ERROR", "[otp-sender] otp send failed region=eu-west, not retrying"),
            (235, "INFO", "[login] fallback=email offered template=otp_fallback_email region=eu-west"),
            (180, "ERROR", "[otp-sender] otp send failed region=eu-west, not retrying"),
            (120, "WARN", "[otp-sender] provider=twilio pool=shared-eu status=429 Too Many Requests region=eu-west"),
            (120, "ERROR", "[otp-sender] otp send failed region=eu-west, not retrying"),
            (60, "ERROR", "[otp-sender] otp send failed region=eu-west, not retrying"),
        ],
        # the second symptom, in another service: the email fallback fills the email queue
        "also": {"notifications": [
            (230, "INFO", "[consumer] queue=email depth=240 oldest_age_s=5"),
            (200, "WARN", "[consumer] queue=email depth=2900 oldest_age_s=210 template=otp_fallback_email volume up"),
            (150, "ERROR", "[smtp] send deferred template=otp_fallback_email status=421 rate limited by relay"),
            (110, "WARN", "[consumer] queue=email depth=6100 oldest_age_s=840 template=otp_fallback_email"),
            (70, "ERROR", "[consumer] queue=email lag over 15m depth=8800 template=otp_fallback_email"),
            (30, "ERROR", "[smtp] send deferred template=otp_fallback_email status=421 rate limited by relay"),
        ]},
    },
}


def _append(settings: Settings, service: str, entries: list, now: datetime) -> int:
    log = settings.logs_dir / f"{service}.log"
    backup = settings.data_dir / "log-backup" / log.name
    if not backup.exists():
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(log, backup)
    lines = [f"{(now - timedelta(seconds=ago)).strftime('%Y-%m-%dT%H:%M:%SZ')} {level} {text}" for ago, level, text in entries]
    with log.open("a") as handle:
        handle.write("\n".join(lines) + "\n")
    return len(lines)


def write(name: str, settings: Settings) -> str:
    scenario = SCENARIOS[name]
    now = datetime.now(UTC).replace(microsecond=0)
    written = _append(settings, scenario["service"], scenario["lines"], now)
    for service, entries in scenario.get("also", {}).items():  # a second symptom in another service
        written += _append(settings, service, entries, now)
    state_file = settings.data_dir / "demo_state.json"  # what fixes this incident, for automation.DemoRunner
    try:
        state = json.loads(state_file.read_text())
    except (FileNotFoundError, ValueError):
        state = {}
    state[scenario["service"]] = {"scenario": name, "started": now.isoformat(), "fixed_by": scenario["fixed_by"],
                                  "healthy": scenario["healthy"], "still_failing": scenario["still_failing"]}
    state_file.write_text(json.dumps(state, indent=2))
    files = ", ".join(f"{name}.log" for name in [scenario["service"], *scenario.get("also", {})])
    return f"Wrote {written} lines ({sum(1 for _, level, _ in scenario['lines'] if level == 'ERROR')} ERROR) to {files}."


def reset(settings: Settings) -> list[str]:
    restored = []
    folder = settings.data_dir / "log-backup"
    for backup in sorted(folder.glob("*.log")) if folder.exists() else []:
        shutil.copy2(backup, settings.logs_dir / backup.name)
        backup.unlink()
        restored.append(backup.name)
    (settings.data_dir / "demo_state.json").unlink(missing_ok=True)
    return restored


def main() -> int:
    parser = argparse.ArgumentParser(description="Write a demo incident into a service log.")
    parser.add_argument("scenario", nargs="?", choices=sorted(SCENARIOS))
    parser.add_argument("--list", action="store_true", help="show the scenarios")
    parser.add_argument("--reset", action="store_true", help="put the logs back as they were")
    args = parser.parse_args()
    settings = Settings()
    if args.list or not (args.scenario or args.reset):
        for name, scenario in SCENARIOS.items():
            print(f"{name:20} {scenario['service']:14} {scenario['about']}")
        return 0
    if args.reset:
        restored = reset(settings)
        print(f"Restored: {', '.join(restored)}" if restored else "Nothing to restore.")
        return 0
    print(write(args.scenario, settings))
    print("Grafana fires the alert in about 1 minute. Then a new case opens in RootIQ.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
