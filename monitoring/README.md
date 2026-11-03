# Monitoring: Grafana dashboard and alerts

Prometheus reads the RootIQ metrics. Grafana shows them on the **RootIQ Agents** dashboard. A Grafana alert
rule watches the service logs. When it fires, Grafana sends the alert to RootIQ, RootIQ opens a case, and the
agent team starts. No person has to create the case.

```
service log  ->  /metrics  ->  Prometheus  ->  Grafana alert rule  ->  RootIQ webhook  ->  case + Jira ticket  ->  agent team
```

## Start

1. Install the two programs one time: `brew install prometheus grafana`
2. Start the RootIQ backend.
3. Start the monitoring: `./monitoring/start.sh`
4. Open the dashboard: http://127.0.0.1:3000/d/rootiq-agents

You can view the dashboard without a sign-in. To change it, sign in as `admin`. The password is
`ROOTIQ_GRAFANA_PASSWORD` in `backend/.env`. Prometheus is at http://127.0.0.1:9090.

Without Homebrew, use Docker: `cd monitoring && ROOTIQ_ALERT_TOKEN=<the value in backend/.env> docker compose up`

## Try the alert flow

```bash
.venv/bin/python -m backend.app.simulate_incident --list            # the scenarios
.venv/bin/python -m backend.app.simulate_incident payments-tls      # write an incident into a service log
```

After about 1 minute, Grafana fires "Service errors rising". A new case opens in RootIQ with the customer
"Grafana alert", and its Jira ticket. The Agent team tab shows "Started by a Grafana alert".
About 5 minutes after the last error, the alert stops and the case gets a note. You resolve the case.

To put the logs back as they were: `.venv/bin/python -m backend.app.simulate_incident --reset`

## What is in this folder

| File | Purpose |
| --- | --- |
| `start.sh` | Starts Prometheus and Grafana with the values from `backend/.env` |
| `prometheus.yml` | Reads `http://127.0.0.1:8011/metrics` every 15 seconds |
| `grafana/grafana.ini` | Grafana settings: port 3000, dark theme, viewing without sign-in |
| `grafana/provisioning/datasources` | The Prometheus data source |
| `grafana/provisioning/dashboards`, `grafana/dashboards` | The RootIQ Agents dashboard |
| `grafana/provisioning/alerting` | The alert rule, the RootIQ contact point and the routing |
| `docker-compose.yml`, `prometheus.docker.yml` | The same setup in Docker |

Grafana creates all of these when it starts. You do not have to set them up by hand.

## The alert rule

**Service errors rising**: `rootiq_service_log_errors_5m > 3` for 30 seconds, for each service. The metric counts the ERROR lines whose time is in the last 5 minutes.

Grafana sends the alert to `POST /api/alerts/grafana` with the token `ROOTIQ_ALERT_TOKEN`. RootIQ refuses
messages without the correct token. The rules for each alert:

- A new alert opens a case, a Jira ticket and an agent run.
- The same alert again, while its case is open, does not open a second case.
- An alert for the same rule and service joins the open case as a note.
- When the alert stops, the case gets a note. The engineer resolves the case.
- If the alert fires again after its case is resolved, a new case opens. It says that the problem came back.

## The metrics

| Metric | Meaning |
| --- | --- |
| `rootiq_cases{status, severity}` | Cases now |
| `rootiq_open_cases_sla{state}` | Open cases: on track, at risk, past SLA |
| `rootiq_agent_runs_total{status, trigger}` | Agent runs, and what started them |
| `rootiq_agent_queue_length` | Runs that wait |
| `rootiq_agent_investigation_seconds` | Investigation time |
| `rootiq_agent_model_calls_total{agent, model, outcome}` | Model calls. "failed" calls went to the local backup |
| `rootiq_agent_tokens_total{agent, direction}` | Tokens |
| `rootiq_agent_tool_calls_total{agent, tool, outcome}` | Tool calls |
| `rootiq_agent_runs_with_warnings_total` | Runs with a guard warning |
| `rootiq_agent_decisions_total{decision}` | Engineer decisions on proposals |
| `rootiq_agent_lessons_total{verdict}` | Diagnoses graded after the fix |
| `rootiq_main_model{provider, model}`, `rootiq_main_model_paused` | The main model, and a quota pause |
| `rootiq_service_log_errors_total{service}`, `rootiq_service_log_errors_5m{service}` | ERROR lines in each service log: all, and of the last 5 minutes |
| `rootiq_alerts_received_total{status, result}` | Alerts from Grafana and what RootIQ did |

The metrics page has counts only. It has no case text and no keys.
