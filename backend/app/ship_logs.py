"""Send the demo service logs to Grafana Loki, and continue to send the new lines.

    .venv/bin/python -m backend.app.ship_logs              # runs until Ctrl+C; monitoring/start.sh starts it
    .venv/bin/python -m backend.app.ship_logs --once       # send what is there, then stop

In a real company, a log agent such as Grafana Alloy sends the logs to Loki. This small program does the same for
the demo logs in backend/seed/logs, so that the Loki data source has real data. Each file is one service. The stream
labels are {service="<file name>", job="rootiq-demo"}. A line keeps the time at its start.
"""

import argparse
import sys
import time
from datetime import UTC, datetime

import httpx

from .config import Settings


def line_time_ns(line: str) -> int:
    try:
        return int(datetime.fromisoformat(line.split(" ", 1)[0].replace("Z", "+00:00")).timestamp() * 1e9)
    except ValueError:
        return int(datetime.now(UTC).timestamp() * 1e9)


def push(client: httpx.Client, url: str, service: str, lines: list[str]) -> None:
    values = sorted([str(line_time_ns(line)), line] for line in lines if line.strip())
    if values:
        client.post(f"{url}/loki/api/v1/push", json={"streams": [
            {"stream": {"service": service, "job": "rootiq-demo"}, "values": values}]}).raise_for_status()


def main() -> int:
    parser = argparse.ArgumentParser(description="Send the demo service logs to Loki.")
    parser.add_argument("--url", default="http://127.0.0.1:3100")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    folder = Settings().logs_dir
    sent: dict[str, int] = {}  # file name -> number of lines already sent
    with httpx.Client(timeout=10) as client:
        while True:
            for path in sorted(folder.glob("*.log")):
                lines = path.read_text().splitlines()
                start = sent.get(path.name, 0)
                if len(lines) < start:
                    start = 0  # simulate_incident --reset put the file back: send it again. Loki drops the copies.
                try:
                    push(client, args.url, path.stem, lines[start:])
                    sent[path.name] = len(lines)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 400:
                        print(f"Loki is not ready (HTTP {exc.response.status_code}). Trying again.", file=sys.stderr)
                        break
                    # 400: Loki refused some lines, usually lines that it already has after a restart. It keeps the others.
                    sent[path.name] = len(lines)
                except httpx.HTTPError as exc:
                    print(f"Loki is not ready ({type(exc).__name__}). Trying again.", file=sys.stderr)
                    break
            if args.once:
                return 0
            time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
