# gmail-agent

A small harness that reads Gmail, extracts email + attachment content with one agent,
decides a next action with a second agent, and applies safe actions (drafts and labels)
after a human approves. Built on LangChain 1.x `create_agent` + LangGraph, with Claude
via `langchain-anthropic`.

```
poll ─► ingest ─► extract (Sonnet 5.5) ─► decide (Opus 5.5) ─► draft_reply | label | archive | needs_human | noop
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
cp .env.example .env          # then put your ANTHROPIC_API_KEY in .env, or set
                              # LLM_BACKEND=claude_code to use your Claude Code login
uv sync
uv run gmail-agent auth       # opens a browser, writes token.json
```

## Usage

```bash
uv run gmail-agent fetch <message_id>              # parse one email, no LLM
uv run gmail-agent dump <message_id> tests/fixtures/x.json   # save raw JSON for tests
uv run gmail-agent process <message_id>            # full pipeline on one email
uv run gmail-agent poll --max 5                    # last 5 unread, once
uv run gmail-agent poll --loop --interval 120      # keep polling
uv run gmail-agent ui                              # http://127.0.0.1:8000, read-only viewer
```

Message ids: open an email in Gmail, the id is not in the URL. Easiest is
`uv run gmail-agent poll --max 3` which prints ids as it goes, or use the Gmail search API
through `GmailClient.list_message_ids(query)`.

## Layout

```
src/gmail_agent/
  config.py            settings from .env
  schemas.py           Email, ExtractedEmail, NextAction (Pydantic contracts between nodes)
  store.py             SQLite table of processed message ids
  gmail/auth.py        OAuth, scopes
  gmail/client.py      Gmail API wrapper + pure parse_message()
  agents/models.py     ChatAnthropic factory (thinking, effort, fallbacks)
  agents/attachments.py  bytes -> text for pdf/docx/csv/txt
  agents/extractor.py  sub-agent 1: create_agent + read_attachment tool
  agents/triage.py     sub-agent 2: create_agent + GmailToolkit search/thread tools
  agents/claude_code.py  same two agents on the Claude Agent SDK (LLM_BACKEND=claude_code)
  graph.py             LangGraph StateGraph: ingest -> extract -> decide -> act
  ui.py                local viewer: input email next to extraction + decision
  cli.py               auth / fetch / dump / process / poll / ui
tests/                 offline tests (fixture JSON, mocked Gmail, stubbed agents)
```

## Safety choices

- The agents never get a send tool. Drafts and labels only, and drafts wait for your `y`.
- Email bodies are injected into prompts behind an "untrusted content" marker, and both
  system prompts say to treat them as data.
- Every message id is recorded in `state.db`; a message is never processed twice.
- LangGraph state is checkpointed in the same SQLite file, so an interrupted run resumes.

## Observability

Set `LANGSMITH_TRACING=true` and `LANGSMITH_API_KEY` in `.env` to see every node, tool
call and token count at <https://smith.langchain.com>.

## Roadmap

- [x] Phase 1-4: Gmail plumbing, extractor, triage, graph, actions, persistence
- [ ] Phase 5: Pub/Sub push (`users.watch`, renew every 7 days) instead of polling
- [ ] Images in attachments through Claude vision
- [ ] Calendar lookups for `schedule` actions
