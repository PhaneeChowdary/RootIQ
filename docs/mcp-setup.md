# Use RootIQ from other AI tools (MCP)

RootIQ has an MCP server (Model Context Protocol). Other AI tools, for example Claude Desktop or Claude Code,
can use it. Then you can ask such a tool to list the open incidents, read service logs, or start an investigation
and tell you the likely cause.

## What the server gives

- Read tools: `list_open_cases`, `get_incident`, `search_similar_incidents`, `list_service_incidents`,
  `read_service_logs`, `search_jira`.
- Investigation tools: `start_investigation`, `get_investigation`, `latest_investigation`, `agent_team_metrics`.

The server has no write tools. It cannot send emails or change Jira. Those actions stay proposals that an engineer
approves in RootIQ.

## Before you start

- Install the project (see "Run it" in the README).
- Start the backend. The investigation tools need it at `ROOTIQ_API_URL` (set in `backend/.env`).
- In the commands below, change `/absolute/path/to/RootIQ` to the folder of your copy of the project.

## Claude Desktop

1. Open `~/Library/Application Support/Claude/claude_desktop_config.json`.
2. Add this entry:

```json
{
  "mcpServers": {
    "rootiq": {
      "command": "/absolute/path/to/RootIQ/.venv/bin/python",
      "args": ["-m", "backend.app.mcp_server"],
      "env": { "PYTHONPATH": "/absolute/path/to/RootIQ" }
    }
  }
}
```

3. Restart Claude Desktop.

## Claude Code

Run this command one time:

```bash
claude mcp add rootiq -e PYTHONPATH=/absolute/path/to/RootIQ -- \
  /absolute/path/to/RootIQ/.venv/bin/python -m backend.app.mcp_server
```

## Test the server without an AI tool

```bash
.venv/bin/python -m backend.app.mcp_server
```

The server waits for MCP messages on standard input. Stop it with Ctrl+C.
