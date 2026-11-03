# Run RootIQ on a server

This guide puts RootIQ on one Linux server with Docker. For one computer and a demo, `./run.sh` is enough (see
the README).

## What runs

| Service | What it does |
|---|---|
| `postgres` | The database: cases, users, agent runs, the job queue and the agent checkpoints |
| `migrate` | Applies the database changes (`backend/app/migrations.py`), then stops |
| `api` | The REST API and WebSockets. It does not run agents |
| `worker` | Runs the agent team. Two by default. Add more with `--scale worker=N` |
| `web` | The website (nginx). It sends `/api` to the API |
| `backup` | Makes a backup every 24 hours and keeps the newest 14 |
| `seed` | Optional (profile `demo`): loads the demo cases |
| `prometheus`, `grafana` | Optional (profile `monitoring`): dashboard and alerts |

The workers share one job queue in PostgreSQL. Two workers never take the same job. When a worker stops (a crash,
an update, a lost server), another worker continues its investigation from the last checkpoint after 90 seconds.
Approved actions that were being sent at that time are never sent again: the run fails, and an engineer checks Gmail
and Jira.

## 1. Prepare

1. Install Docker with the Compose plugin on the server.
2. Copy the project to the server.
3. Make the settings file, then set the values:

   ```bash
   cp backend/.env.example backend/.env
   ```

   - `ROOTIQ_AUTH_USERNAME` and `ROOTIQ_AUTH_PASSWORD`: the first admin account. Use a long password.
   - A model: a cloud key (set it later on **Settings > Models**), or Ollama on the server.
   - Optional: Jira, Gmail, single sign-on (`ROOTIQ_OIDC_...`).

4. Make a file named `.env` next to `docker-compose.yml`. Docker Compose reads it:

   ```bash
   ROOTIQ_DB_PASSWORD=<a long random password, letters and digits only>
   ROOTIQ_PUBLIC_URL=https://rootiq.example.com
   ROOTIQ_COOKIE_SECURE=true
   ```

   Letters and digits only: the password is part of the database address.

## 2. Start

```bash
docker compose up -d --build
docker compose ps                               # all services "running" or "healthy"; migrate "exited (0)"
docker compose --profile demo run --rm seed     # optional: the demo cases
```

The website is at port 8080. Sign in with the admin account from `backend/.env`. Then add people on
**Settings > Users**.

## 3. HTTPS

Put a proxy with HTTPS in front of port 8080. Example with Caddy (it gets the certificate itself):

```
rootiq.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

With HTTPS, keep `ROOTIQ_COOKIE_SECURE=true`. Open only ports 80 and 443 in the firewall. The database and the
API have no open ports.

## 4. Scale the workers

```bash
docker compose up -d --scale worker=4                  # 4 worker processes
ROOTIQ_WORKER_THREADS=3 docker compose up -d worker # 3 threads in each worker
```

Each thread runs one investigation at a time. The model provider's rate limit is often the real limit: more workers
do not help when the provider refuses calls. **Settings > Security** shows the live workers and the runs that wait.

Workers can also run on other servers. Start the same image with the same `backend/.env`, the same
`ROOTIQ_DATABASE_URL`, and the command `python -m backend.app.worker`. They also need the `/data` files (the
encryption key and the settings files): use shared storage, or copy `secret.key`, `data_sources.json` and
`model_settings.json` to each server.

## 5. Backups and restore

The `backup` service writes `rootiq-<time>.tar.gz` into the `appdata` volume (`/data/backups`) every 24 hours. Copy
the backups to another place every day, for example object storage.

**The encryption key is not in the backups.** Copy `/data/secret.key` to a safe place one time, apart from the
backups:

```bash
docker compose cp api:/data/secret.key ./rootiq-secret.key   # then store it in a password manager or vault
```

Check that a backup can be read:

```bash
docker compose exec api python -m backend.app.backup --list
docker compose exec api python -m backend.app.backup --verify /data/backups/<file>.tar.gz
```

Restore (this replaces the data):

```bash
docker compose stop api worker web
mkdir restore && tar -xzf <file>.tar.gz -C restore
docker compose cp restore/rootiq.pgdump postgres:/tmp/rootiq.pgdump
docker compose exec postgres pg_restore --clean --if-exists --no-owner -U rootiq -d rootiq /tmp/rootiq.pgdump
docker compose up -d
```

Try a restore on a test server before you need one.

## 6. Update

```bash
git pull
docker compose up -d --build     # "migrate" applies new database changes before the API and the workers start
```

A worker gets 70 seconds to finish its current step at a stop. An investigation that does not finish in that time
continues on another worker.

## 7. Move from SQLite to PostgreSQL

To keep the cases, users and runs from a computer that used SQLite:

```bash
.venv/bin/python -m backend.app.copy_data --to postgresql://rootiq:<password>@<host>:5432/rootiq
```

Copy `backend/data/secret.key`, `data_sources.json` and `model_settings.json` to the server's `/data` too: the saved
secrets need the same key. Runs that wait for approval keep their proposals, but cannot continue (their checkpoints
stay in SQLite). Start new runs for them.

## Checks

- `GET /healthz`: the API process answers.
- `GET /readyz`: the API can reach the database (HTTP 503 if not). Docker uses it.
- `GET /metrics`: Prometheus metrics, including `rootiq_agent_queue_length` and `rootiq_agent_jobs_running`.
- **Settings > Security**: masking, encryption, sign-in locks, retention, backups, database version and workers.

## Build on a Mac without Docker Desktop

Colima gives Docker on a Mac. Install the Compose and Buildx plugins too:

```bash
brew install colima docker docker-compose docker-buildx
colima start --cpu 4 --memory 6
ROOTIQ_DB_PASSWORD=... docker compose build
```

- If `docker compose` is an unknown command, link the plugin: `mkdir -p ~/.docker/cli-plugins && ln -s
  "$(brew --prefix)/opt/docker-compose/bin/docker-compose" ~/.docker/cli-plugins/`. Do the same for `docker-buildx`.
- If the build stops with `docker-credential-desktop: executable file not found`, the Docker settings still name the
  Docker Desktop credential helper. Remove the line `"credsStore": "desktop"` from `~/.docker/config.json`.
- `colima stop` frees the memory of the Docker machine when you do not use it.
