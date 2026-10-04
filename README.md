# gmail-agent

A harness that reads Gmail, extracts email and attachment content with one agent, decides
a next action with a second agent, checks that decision against code-enforced policy, and
applies safe actions (labels, archive, drafts) with a human approving anything outbound.
Built on LangGraph, with Claude through either LangChain (`api` backend) or the Claude
Agent SDK (`claude_code` backend).

```
poll ─► ingest ─► extract ─► decide ─► guard ─► draft_reply | label | archive | needs_human | noop
                  (agent)    (agent)   (policy)  (human approves drafts and needs_human)
```

## One-time setup

### 1. Google Cloud (about 10 minutes, browser)

1. Go to <https://console.cloud.google.com/>, create a project (e.g. `gmail-agent`).
2. **APIs & Services → Library** → enable **Gmail API**.
3. **APIs & Services → OAuth consent screen** → External → fill name/email → add yourself
   under **Test users**.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID** →
   Application type **Desktop app** → Download JSON → save as `credentials.json` in this folder.

While the consent screen is in *Testing* mode, the refresh token expires after 7 days and
you will need to run `gmail-agent auth` again. Publishing the app removes that limit.

### 2. Python env

```bash
cp .env.example .env          # pick LLM_BACKEND; for "api" put your ANTHROPIC_API_KEY in .env
cp src/gmail_agent/harness/owner.example.md owner.md   # optional: your name, languages
uv sync
uv run gmail-agent auth       # opens a browser, writes token.json
uv run gmail-agent check      # validates the agent and skill files
```

## Usage

```bash
uv run gmail-agent process <message_id> --dry-run   # decide, change nothing in Gmail
uv run gmail-agent process <message_id>             # full pipeline on one email
uv run gmail-agent poll --max 5                     # last 5 unread, once
uv run gmail-agent poll --loop --interval 120       # keep polling (add --dry-run to observe)
uv run gmail-agent ui                               # http://127.0.0.1:8000, Process buttons + approvals
uv run gmail-agent eval                             # score the agents on saved emails (offline)
uv run gmail-agent check --prompts                  # print the exact system prompts
```

## How the harness is organised

```
src/gmail_agent/
  harness/                    agent behaviour, in Markdown (edit these, not Python)
    agents/extractor.md         always-on prompt + frontmatter: model, tools, skills, output
    agents/triage.md
    skills/<name>/SKILL.md      on-demand instructions; references/ for extra files
    owner.example.md            template for owner.md (your profile, appended to prompts)
  harness_loader.py           parses + validates agents/skills, renders the system prompt
  tools.py                    tools defined once: read_attachment, search_gmail,
                              get_gmail_thread, load_skill, read_skill_file
  backends/                   api (LangChain + API key) and claude_code (Agent SDK + login)
  policy.py                   guardrails enforced after the triage agent decides
  graph.py                    LangGraph pipeline + run_message() used by CLI, poller and UI
  evals.py                    offline eval runner and scoring
  store.py                    SQLite run history (done / waiting / failed / dry_run)
  gmail/                      OAuth and the Gmail API wrapper (with retries)
  ui.py, cli.py               web UI and command line
evals/cases-synthetic/        committed eval cases; your real ones go in evals/cases/ (gitignored)
```

### Agents

Each `harness/agents/<name>.md` starts with YAML frontmatter:

```yaml
name: triage                # must match the file name
description: ...
model: triage               # which model/effort settings to use: extract | triage
tools: [search_gmail, get_gmail_thread]
skills: [triage-policy, reply-drafting, labeling]
output: NextAction          # Pydantic schema the agent must return
```

The Markdown body is the always-on system prompt. Safety rules (email content is
untrusted data) live here, never in a skill, so they always apply.

### Skills

A skill is a folder with a `SKILL.md` (frontmatter `name` and `description`, then
instructions) and optional `references/` files. An agent's prompt lists only each skill's
name and description; the agent calls `load_skill` to read the full instructions when it
needs them, and `read_skill_file` for references. This is the same progressive-disclosure
pattern as Claude Code and deepagents skills, implemented once so both backends behave
the same.

To add a skill: create `harness/skills/<name>/SKILL.md`, add `<name>` to an agent's
`skills:` list, run `gmail-agent check`, then `gmail-agent eval` to see the effect.

The `labeling` skill's `labels:` frontmatter must be a subset of `ALLOWED_LABELS`.

## Safety

- **Nothing is ever sent.** No agent has a send tool, and the Gmail client has no send
  method. Drafts are saved only after you approve them.
- **Policy is enforced in code** (`policy.py`), whatever the model says:
  - below `MIN_CONFIDENCE` the email goes to you instead
  - labels outside `ALLOWED_LABELS` are dropped
  - empty drafts and `schedule` go to you
  - every override is shown in the UI and logged
- **Dry-run** (`--dry-run` or `DRY_RUN=true`) runs the agents and the policy, but never
  changes Gmail. Dry-run results don't stop a later real run.
- **Email content is treated as data.** It's marked as untrusted in every prompt and tool
  result. The prompt-injection eval case checks that the agent sends such an email to a
  human.
- **Each message runs once.** Every message id is recorded in `state.db`. Failed runs are
  recorded with their error, and `poll --retry-failed` re-runs them.
- **Interrupted runs resume.** LangGraph state is checkpointed in the same SQLite file.

## Evals

```bash
uv run gmail-agent dump <message_id> --case my-flight   # snapshot a real email + attachments
$EDITOR evals/cases/my-flight/expected.yaml             # what a correct decision is
uv run gmail-agent eval                                 # all cases, configured backend
uv run gmail-agent eval --backend claude_code --repeat 3 --case flight
```

`expected.yaml` takes these fields:

- `kind`: the acceptable final action or actions
- `labels_include`: labels that must be applied
- `must_not`: unsafe outcomes; any match fails the run
- `facts`: strings the extraction must contain

`eval` prints one line per run and a summary (accuracy, violations, fact recall, cost,
latency). It writes a JSON report to `evals/results/` and exits non-zero on any violation
or when accuracy falls below `--min-accuracy`. Run it after every prompt or skill change.

## Observability

Each run logs one JSON line to stderr with the proposed and final actions, policy
overrides, tool and skill calls, cost and duration. The UI shows the same information
per email. Set `LANGSMITH_TRACING=true` and `LANGSMITH_API_KEY` in `.env` for full traces
of the `api` backend at <https://smith.langchain.com>.

## Roadmap

- [ ] Pub/Sub push (`users.watch`, renew every 7 days) instead of polling
- [ ] Images in attachments through Claude vision
- [ ] Calendar integration for `schedule` actions (currently sent to a human)
