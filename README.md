# Kanboard Agent Worker

A pull-based worker for running local CLI agents from Kanboard tasks.

Each worker is a Kanboard user. It polls configured boards, claims tasks assigned
to that user from a configured todo column, moves them to a working column,
runs an ACP-compatible local agent such as Codex or Claude, posts the agent's
final response back to the card, and moves the task to done or blocked.

Kanboard is the shared state layer. There is no central dispatcher.

## Status

This is the initial worker skeleton:

- YAML configuration for server credentials, board IDs, and per-board column names
- Kanboard JSON-RPC client using user credentials or a personal access token
- Worker lifecycle for poll, claim, execute, comment, complete, and block
- ACP agent execution for Codex, Claude, or an explicitly configured ACP command
- CLI commands for config/API checks and continuous polling

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

roster:
  - name: codex
    description: Repository-oriented coding agent
  - name: claude
    description: General implementation agent

agent:
  name: codex
  pwd: .
  system_prompt: |
    Follow the card instructions and keep the final response concise.
  command:
    - codex-acp
  timeout_seconds: 3600

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

For built-in agents, `agent.name` selects the default ACP command:

- `codex`: starts `codex-acp` by default and communicates through the Agent Client Protocol.
- `claude`: starts `claude-agent-acp` by default and communicates through the Agent Client Protocol.

`agent.command` can override the ACP executable. For any other `agent.name`,
`agent.command` is required and must point at an ACP-compatible process. ACP
sessions receive a Kanboard MCP server with tools to list, download, upload, and
delete task attachments; create subtasks; and move cards to the configured
`todo`, `working`, `blocked`, or `done` columns.

All agents receive the same Jinja-rendered prompt template. It includes the
worker username, card fields, task metadata, the visible Kanboard comment
conversation, the configured `roster`, the task description, and
`agent.system_prompt`. The template
tells the agent that its final response will be posted as a Kanboard card
comment. Larger artifacts should be written to files and attached to the card
with the Kanboard attachment tools. Successful whole-card work moves to the
configured done column by default. Agents should call the Kanboard `move_column`
tool when a card should be blocked or moved somewhere other than the default
successful completion path.

Codex and Claude metadata is stored per Kanboard worker identity:

```text
kanboard_worker.{server.user}.session_id
```

Subtask agent metadata is stored on the parent task with the subtask id:

```text
kanboard_worker.{server.user}.subtask.{subtask_id}.session_id
```

## Run

Check connectivity and board column configuration:

```bash
kanboard-agent-worker --config config.yml check
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
```

If `## Spec` is missing, the worker sends the whole description to the agent.

## Subtasks

Workers always look for assigned todo subtasks before claiming whole cards.
Subtasks can be claimed from parent cards in any configured board column. When a
worker starts a subtask, it marks the subtask in progress, starts the Kanboard
subtask timer, and comments on the parent task. When the subtask finishes, the
worker stops the timer, posts the result to the parent task comments, and marks
the subtask done.

On startup recovery, the worker pauses timers for its in-progress subtasks,
returns those subtasks to todo, and posts the recovery comment on the parent
task. Whole-card recovery still moves the worker's assigned in-process cards
back to the todo column.

Whole cards with pending subtasks are left in the queue and are not claimed
until all subtasks are done. This allows subtasks to complete before the parent
card is picked up for final task-level work.

Agents can create one or more subtasks by calling the Kanboard `add_subtask`
tool. The optional `assignee` argument should match a Kanboard username, usually
one of the configured roster entries. When a whole-card agent creates pending
subtasks, the worker sees those subtasks before auto-completing the parent and
leaves the parent card in its current column.

## Worker Lifecycle

1. On startup, move this worker's assigned working-column tasks back to the todo
   column with a recovery comment, and pause/requeue this worker's in-progress
   subtasks.
2. Poll configured boards.
3. Count tasks assigned to the worker in the working column plus assigned
   in-progress subtasks.
4. Claim assigned todo subtasks from any column until concurrency is full.
5. Claim assigned whole tasks from the todo column when they have no pending
   subtasks.
6. Move claimed whole tasks to the working column.
7. Build one agent prompt from card metadata, conversation, worker identity, task
   description, and system prompt.
8. Run the selected ACP agent from `agent.pwd`.
9. Save the ACP session id in Kanboard task metadata.
10. Give ACP agents Kanboard tools for attachments, subtasks, and configured
    column moves.
11. Post the final response to Kanboard comments.
12. Move successful whole-task work to done unless the agent already moved the
    card or the card has pending subtasks. Failed responses are blocked.

## API Notes

The implementation uses Kanboard JSON-RPC 2.0 via POST requests, `getBoard` to
read swimlanes/columns/tasks, `moveTaskPosition` for column moves,
`createComment` for progress comments, `getAllSubtasks` and `updateSubtask` for subtask lifecycle, `setSubtaskStartTime` and
`setSubtaskEndTime` for timers, task file methods for attachments, and `getMe`
to resolve the authenticated user's numeric ID for comments.
