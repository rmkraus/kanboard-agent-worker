# Kanboard Agent Worker

A pull-based worker for running local CLI agents from Kanboard tasks.

Each worker is a Kanboard user. It polls configured boards, claims tasks assigned
to that user from a configured todo column, moves them to a working column,
runs a local command such as `codex`, `hermes`, or `claude`, comments progress
back to the card, and moves the task to done or blocked.

Kanboard is the shared state layer. There is no central dispatcher.

## Status

This is the initial worker skeleton:

- YAML configuration for server credentials, board IDs, and per-board column names
- Kanboard JSON-RPC client using user credentials or a personal access token
- Worker lifecycle for poll, claim, execute, comment, complete, and block
- Agent adapters for Codex, Claude, and generic subprocess agents
- CLI commands for config/API checks, one-shot processing, and continuous polling

## Install

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e ".[dev]"
```

## Configuration

Copy the example and put real secrets in `config.yml`, which is ignored by git.

```bash
cp config.example.yml config.yml
```

Example:

```yaml
server:
  user: admin
  token: admin
  url: http://localhost:8080

worker:
  max_concurrency: 1
  poll_interval: 10

agent:
  name: codex
  pwd: .
  command:
    - codex
  timeout_seconds: 3600
  pass_task_on_stdin: true

boards:
  - id: 1
    todo: "Intake"
    working: "In Process"
    blocked: "Escalate"
    done: "Complete"
```

Environment variables can override machine-local values:

```bash
KANBOARD_URL=http://localhost:8080
KANBOARD_USER=admin
KANBOARD_TOKEN=admin
WORKER_MAX_CONCURRENCY=2
WORKER_POLL_INTERVAL=10
AGENT_PWD=/path/to/checkout
```

Kanboard's API endpoint is `/jsonrpc.php`; if the configured URL is the server
root, the worker appends that path automatically. User API auth uses HTTP Basic
auth with the Kanboard username and either password or personal access token.

`agent.pwd` is the working directory used when starting the local agent command.
Relative paths are resolved from the config file's directory. The directory must
already exist. Older configs may use `agent.cwd`, but `agent.pwd` is preferred.

For built-in adapters, `agent.name` selects behavior:

- `codex`: starts with `codex exec --json`, stores the emitted thread UUID in task metadata, and resumes by UUID.
- `claude`: first-pass Claude wrapper using Claude Code print mode and session IDs.
- anything else: generic subprocess runner using `agent.command`.

Codex and Claude metadata is stored per Kanboard worker identity:

```text
kanboard_agent.{server.user}.thread_id
```

## Run

Check connectivity and board column configuration:

```bash
kanboard-agent-worker --config config.yml check
```

Process currently available work once and exit:

```bash
kanboard-agent-worker --config config.yml once
```

Run continuously:

```bash
kanboard-agent-worker --config config.yml run
```

## Card Format

Task descriptions can use this markdown convention:

```markdown
## Spec
Natural language instructions for the agent.

## Config
agent: codex
max_tokens: 4000
context: optional paths or context

## Output
The worker replaces this section with the final summary.
```

If `## Spec` is missing, the worker sends the whole description to the agent.

## Worker Lifecycle

1. Poll configured boards.
2. Count tasks assigned to the worker in the working column.
3. Claim assigned tasks from the todo column until concurrency is full.
4. Move claimed tasks to the working column.
5. Ensure the task has an agent thread id in Kanboard task metadata.
6. Run the local agent command from `agent.pwd`.
7. Capture stdout, stderr, exit code, and parsed JSON events when available.
8. Feed that run bundle back to the same agent for a concise card summary.
9. Post only the summary to the Kanboard comments and update `## Output`.
10. Move successful tasks to done; move failed tasks to blocked.

## API Notes

The implementation uses Kanboard JSON-RPC 2.0 via POST requests, `getBoard` to
read swimlanes/columns/tasks, `moveTaskPosition` for column moves, `updateTask`
for description updates, `createComment` for progress comments, and `getMe` to
resolve the authenticated user's numeric ID for comments.
