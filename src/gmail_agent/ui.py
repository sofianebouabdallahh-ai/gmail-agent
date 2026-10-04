"""Local web UI: the live inbox, a Process button per email, and what the pipeline did.

The inbox is listed read-only through the Gmail API; results come from the LangGraph
checkpoints in state.db (one thread per message). Clicking Process runs the same graph as
`gmail-agent process` in a background thread, and the approval steps (drafts, needs_human)
are answered with forms on the page instead of in the terminal.

Email content is untrusted: every value is HTML-escaped before it is written to the page.
"""

from __future__ import annotations

import sqlite3
import threading
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from gmail_agent.gmail import GmailClient
from gmail_agent.schemas import ActionKind, Attachment, Email, ExtractedEmail, NextAction
from gmail_agent.store import ProcessedStore

CSS = """
:root { --bg:#f6f7f9; --card:#fff; --text:#1c1f23; --muted:#6b7280; --line:#e3e6ea;
        --accent:#2563eb; --sel:#eaf1ff; --ok:#15803d; --warn:#b45309; --bad:#b91c1c; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#15171a; --card:#1e2125; --text:#e6e8eb; --muted:#9aa2ad; --line:#30353b;
          --accent:#7aa7ff; --sel:#25324a; --ok:#4ade80; --warn:#fbbf24; --bad:#f87171; }
}
* { box-sizing:border-box; }
body { margin:0; font:14px/1.5 system-ui,sans-serif; background:var(--bg); color:var(--text);
       display:grid; grid-template-columns:340px 1fr; height:100vh; }
nav { border-right:1px solid var(--line); overflow-y:auto; background:var(--card); }
nav h1 { font-size:15px; margin:0; padding:14px 16px; border-bottom:1px solid var(--line); }
nav h4 { margin:0; padding:10px 16px 6px; font-size:11px; letter-spacing:.06em;
         text-transform:uppercase; color:var(--muted); border-bottom:1px solid var(--line); }
nav .row { display:flex; align-items:center; gap:8px; padding-right:12px;
           border-bottom:1px solid var(--line); }
nav .row.sel { background:var(--sel); }
nav .row a { flex:1; min-width:0; display:block; padding:10px 0 10px 16px; color:inherit;
             text-decoration:none; }
nav .subj { font-weight:600; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
nav .from { color:var(--muted); font-size:12px; white-space:nowrap; overflow:hidden;
            text-overflow:ellipsis; }
main { overflow-y:auto; padding:20px; display:grid; grid-template-columns:1fr 1fr; gap:20px;
       align-content:start; }
section { background:var(--card); border:1px solid var(--line); border-radius:8px;
          padding:16px 18px; min-width:0; }
section + section { margin-top:20px; }
h2 { font-size:12px; letter-spacing:.06em; text-transform:uppercase; color:var(--muted);
     margin:0 0 12px; }
h3 { font-size:13px; margin:18px 0 6px; }
dl { display:grid; grid-template-columns:max-content 1fr; gap:2px 12px; margin:0; }
dt { color:var(--muted); } dd { margin:0; overflow-wrap:anywhere; }
pre { white-space:pre-wrap; overflow-wrap:anywhere; font:13px/1.5 ui-monospace,monospace;
      background:var(--bg); border:1px solid var(--line); border-radius:6px; padding:12px;
      margin:8px 0 0; }
textarea { width:100%; min-height:160px; font:13px/1.5 ui-monospace,monospace; color:var(--text);
           background:var(--bg); border:1px solid var(--line); border-radius:6px; padding:10px; }
ul { margin:4px 0; padding-left:20px; }
form { margin:0; }
button { font:inherit; font-size:13px; padding:5px 12px; border-radius:6px; cursor:pointer;
         border:1px solid var(--accent); background:var(--accent); color:var(--card); }
button.secondary { background:transparent; color:var(--text); border-color:var(--line); }
button:disabled { opacity:.5; cursor:default; }
.actions { display:flex; gap:8px; margin-top:10px; flex-wrap:wrap; }
.badge { display:inline-block; padding:1px 8px; border-radius:10px; font-size:12px;
         border:1px solid var(--line); color:var(--accent); margin-top:4px; }
.badge.warn { color:var(--warn); } .badge.ok { color:var(--ok); }
.badge.muted { color:var(--muted); } .badge.bad { color:var(--bad); }
.notice { margin:0; padding:10px 16px; color:var(--warn); border-bottom:1px solid var(--line); }
.error { color:var(--bad); }
.hint { color:var(--muted); font-size:12px; margin:8px 0 0; }
.empty { color:var(--muted); }
.body { white-space:pre-wrap; overflow-wrap:anywhere; margin:0; }
iframe.mail { width:100%; height:70vh; border:1px solid var(--line); border-radius:6px;
              background:#fff; display:block; }
.body-head { display:flex; justify-content:space-between; align-items:baseline; }
.body-head a, .att a { color:var(--accent); }
.att { list-style:none; padding:0; margin:4px 0; }
.att li { padding:6px 0; border-bottom:1px solid var(--line); }
.att li:last-child { border-bottom:0; }
.thumb { display:block; max-width:100%; max-height:220px; margin-top:6px; border-radius:4px;
         border:1px solid var(--line); }
@media (max-width:900px) { body { display:block; height:auto; } main { display:block; }
  main > * { margin-bottom:16px; } nav { max-height:45vh; } }
"""


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
def load_runs(saver: SqliteSaver) -> list[dict]:
    """Latest pipeline state of every processed message, newest first."""
    try:
        rows = saver.conn.execute("SELECT DISTINCT thread_id FROM checkpoints").fetchall()
    except sqlite3.OperationalError:  # nothing has run yet, tables do not exist
        return []
    runs = []
    for (thread_id,) in rows:
        tup = saver.get_tuple({"configurable": {"thread_id": thread_id}})
        if tup is None or "message_id" not in tup.checkpoint["channel_values"]:
            continue
        run = dict(tup.checkpoint["channel_values"])
        run["ts"] = tup.checkpoint["ts"]
        run["waiting"] = any(w[1] == "__interrupt__" for w in tup.pending_writes or [])
        runs.append(run)
    runs.sort(key=lambda r: r["ts"], reverse=True)
    return runs


def load_inbox(client: GmailClient | None, query: str, max_results: int,
               cache: dict[str, Email]) -> tuple[list[Email], str]:
    """Read-only: list messages matching `query`. Emails are cached by id, so a
    reload only fetches messages it has not seen before."""
    if client is None:
        return [], "Gmail not connected: showing processed emails only."
    try:
        ids = client.list_message_ids(query, max_results)
        for mid in ids:
            if mid not in cache:
                cache[mid] = client.get_email(mid)
        return [cache[mid] for mid in ids], ""
    except Exception as exc:  # expired token, network: still show processed runs
        return [], f"Could not load Gmail ({exc.__class__.__name__}). Try `gmail-agent auth`."


class Runner:
    """Runs the graph for one message at a time in a background thread.

    Same steps as the CLI's `_process_one`: invoke, stop at an interrupt (the page shows
    the approval form), resume with the reviewer's answer, record the id when finished.
    """

    def __init__(self, graph, store: ProcessedStore):
        self.graph, self.store = graph, store
        self.lock = threading.Lock()  # one pipeline run at a time
        self.running: set[str] = set()
        self.errors: dict[str, str] = {}

    def start(self, message_id: str, resume=None) -> None:
        if message_id in self.running:
            return
        self.running.add(message_id)
        self.errors.pop(message_id, None)
        threading.Thread(target=self._run, args=(message_id, resume), daemon=True).start()

    def _run(self, message_id: str, resume) -> None:
        config = {"configurable": {"thread_id": f"msg-{message_id}"}}
        try:
            with self.lock:
                if resume is None:
                    self.graph.invoke({"message_id": message_id}, config=config)
                else:
                    self.graph.invoke(Command(resume=resume), config=config)
                state = self.graph.get_state(config)
                if not state.next:  # finished, not paused at an approval
                    action = state.values.get("action")
                    self.store.mark(message_id, action.kind.value if action else None,
                                    action.model_dump(mode="json") if action else None)
        except Exception as exc:
            self.errors[message_id] = f"{exc.__class__.__name__}: {exc}"
        finally:
            self.running.discard(message_id)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _dl(pairs: list[tuple[str, object]]) -> str:
    rows = "".join(f"<dt>{escape(k)}</dt><dd>{escape(str(v))}</dd>" for k, v in pairs if v)
    return f"<dl>{rows}</dl>"


def _ul(title: str, items: list[str]) -> str:
    if not items:
        return ""
    return f"<h3>{escape(title)}</h3><ul>" + "".join(f"<li>{escape(i)}</li>" for i in items) + "</ul>"


def _post_form(path: str, message_id: str, inner: str) -> str:
    return (f'<form method="post" action="{path}">'
            f'<input type="hidden" name="id" value="{escape(message_id)}">{inner}</form>')


# Attachment types the browser may display inline. Everything else (HTML, SVG, scripts,
# Office files...) is served as a download, so it can never run on this page's origin.
INLINE_TYPES = {"application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp"}


def _size(n: int) -> str:
    for unit in ("bytes", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def _real_attachments(email: Email) -> list[tuple[int, Attachment]]:
    """(index, attachment) pairs, minus inline images the body already shows (as Gmail does)."""
    return [(i, a) for i, a in enumerate(email.attachments)
            if not (a.content_id and f"cid:{a.content_id}" in email.body_html)]


def _attachments_html(email: Email) -> str:
    items = ""
    for i, a in _real_attachments(email):
        url = f"/attachment?id={quote(email.id)}&n={i}"
        inline = a.mime_type in INLINE_TYPES
        link = (f'<a href="{url}" target="_blank" rel="noopener">{escape(a.filename)}</a>'
                if inline else f'<a href="{url}" download>{escape(a.filename)}</a>')
        hint = f"{a.mime_type} · {_size(a.size)} · {'open' if inline else 'download'}"
        thumb = f'<img class="thumb" src="{url}" alt="" loading="lazy">' if a.mime_type.startswith("image/") and inline else ""
        items += f'<li>{link} <span class="hint">{escape(hint)}</span>{thumb}</li>'
    if not items:
        return ""
    return f'<h3>Attachments ({items.count("<li>")})</h3><ul class="att">{items}</ul>'


def _body_html(email: Email, show_images: bool, origin: str) -> str:
    if not email.body_html:
        return f'<h3>Body</h3><p class="body">{escape(email.body_text)}</p>'
    # The HTML is rendered in a sandboxed iframe: no scripts, no forms, no access to this
    # page. Remote content is blocked by CSP unless asked for, since remote images are
    # often tracking pixels that tell the sender the email was opened.
    img_src = f"data: {origin}" + (" https: http:" if show_images else "")
    csp = f"default-src 'none'; style-src 'unsafe-inline' https:; img-src {img_src}; font-src data:"
    html = email.body_html
    for i, a in enumerate(email.attachments):  # inline images: cid:<id> -> this server
        if a.content_id:
            html = html.replace(f"cid:{a.content_id}", f"{origin}/attachment?id={quote(email.id)}&n={i}")
    doc = (f'<meta http-equiv="Content-Security-Policy" content="{csp}">'
           f'<base target="_blank">{html}')
    toggle = (f'<a href="/?id={quote(email.id)}">Hide remote images</a>' if show_images
              else f'<a href="/?id={quote(email.id)}&amp;images=1">Show remote images</a>')
    return (f'<div class="body-head"><h3>Body</h3><span class="hint">{toggle}</span></div>'
            '<iframe class="mail" sandbox="allow-popups allow-popups-to-escape-sandbox" '
            f'referrerpolicy="no-referrer" srcdoc="{escape(doc)}"></iframe>')


def _email_html(email: Email | None, show_images: bool = False, origin: str = "") -> str:
    if email is None:
        return '<p class="empty">Email was not fetched.</p>'
    out = _dl([("Subject", email.subject), ("Cc", ", ".join(email.cc))])
    return out + _attachments_html(email) + _body_html(email, show_images, origin)


def _extracted_html(ex: ExtractedEmail | None) -> str:
    if ex is None:
        return '<p class="empty">No extraction yet.</p>'
    out = _dl([("Sender", f"{ex.sender_name} <{ex.sender_email}>"), ("Intent", ex.intent),
               ("Summary", ex.summary)])
    out += _ul("Requests", ex.requests) + _ul("Deadlines", ex.deadlines) + _ul("Entities", ex.entities)
    for a in ex.attachments:
        out += f"<h3>{escape(a.filename)}</h3><p>{escape(a.summary)}</p>" + _ul("Key facts", a.key_facts)
    return out


def _approval_html(run: dict, busy: bool) -> str:
    """The form that answers the graph's `interrupt` for this message."""
    action: NextAction = run["action"]
    mid, disabled = run["message_id"], " disabled" if busy else ""
    if action.kind == ActionKind.draft_reply:
        email = run.get("email")
        to = f"To: {email.sender} · Subject: Re: {email.subject}" if email else ""
        return "<h3>Approve draft</h3>" + _post_form("/approve", mid, (
            f'<p class="hint">{escape(to)}</p>'
            f'<textarea name="body">{escape(action.draft_reply or "")}</textarea>'
            '<div class="actions">'
            f'<button name="decision" value="approve"{disabled}>Save as Gmail draft</button>'
            f'<button class="secondary" name="decision" value="reject"{disabled}>Reject</button>'
            '</div><p class="hint">Saves a draft in the thread. Nothing is sent.</p>'
        ))
    return "<h3>Needs your attention</h3>" + _post_form("/note", mid, (
        '<textarea name="note" style="min-height:80px" placeholder="Note for the record"></textarea>'
        f'<div class="actions"><button{disabled}>Done</button></div>'
    ))


def _action_html(run: dict, busy: bool) -> str:
    action: NextAction | None = run.get("action")
    if action is None:
        return '<p class="empty">No decision yet.</p>'
    out = _dl([
        ("Action", action.kind.value), ("Priority", action.priority.value),
        ("Confidence", f"{action.confidence:.2f}"), ("Labels", ", ".join(action.labels)),
        ("Due by", action.due_by), ("Reasoning", action.reasoning),
    ])
    if run["waiting"]:
        return out + _approval_html(run, busy)
    if run.get("outcome"):
        out += f'<h3>Outcome</h3><span class="badge ok">{escape(run["outcome"])}</span>'
    if action.draft_reply:
        out += f"<h3>Draft reply</h3><pre>{escape(action.draft_reply)}</pre>"
    return out


def _process_button(message_id: str, busy: bool, label: str = "Process") -> str:
    disabled = " disabled" if busy else ""
    return _post_form("/process", message_id, f"<button{disabled}>{escape(label)}</button>")


def _nav_item(r: dict, selected: bool, runner: Runner | None) -> str:
    email, action, mid = r.get("email"), r.get("action"), r["message_id"]
    running = runner is not None and mid in runner.running
    failed = runner is not None and mid in runner.errors
    if running:
        kind, cls = "processing…", "warn"
    elif failed:
        kind, cls = "failed", "bad"
    elif r["waiting"]:
        kind, cls = f"{action.kind.value} · needs you", "warn"
    elif action:
        kind, cls = action.kind.value, ""
    else:
        kind, cls = "not processed", "muted"
    attach = ""
    if email is not None and _real_attachments(email):
        attach = ' <span class="badge">contains attachment</span>'
    button = ""
    if runner is not None and not r.get("ts") and not running:
        button = _process_button(mid, runner.lock.locked())
    return (
        f'<div class="row{" sel" if selected else ""}">'
        f'<a href="/?id={quote(mid)}">'
        f'<div class="subj">{escape(email.subject if email else mid)}</div>'
        f'<div class="from">{escape(email.sender if email else "")}</div>'
        f'<span class="badge {cls}">{escape(kind)}</span>{attach}</a>{button}</div>'
    )


def _results_html(run: dict, runner: Runner | None) -> str:
    mid = run["message_id"]
    running = runner is not None and mid in runner.running
    busy = runner is None or runner.lock.locked()
    error = ""
    if runner is not None and mid in runner.errors:
        error = f'<p class="error">Last run failed: {escape(runner.errors[mid])}</p>'
    if running:
        return ('<section><h2>Result</h2><p>Processing… extraction and triage usually take '
                "15–30 seconds. This page refreshes on its own.</p></section>")
    if not run.get("ts"):
        if runner is None:
            body = "<p class=\"empty\">Not processed yet. Run:</p>"
            body += f"<pre>uv run gmail-agent process {escape(mid)}</pre>"
        else:
            body = (
                '<p class="empty">Not processed yet.</p><div class="actions">'
                f"{_process_button(mid, busy, 'Process this email')}</div>"
                '<p class="hint">Runs extraction and triage. Labels and archiving are applied '
                "right away; drafts and needs-human decisions wait for you here.</p>"
            )
        return f"<section><h2>Result</h2>{error}{body}</section>"
    return (
        f"<section><h2>Result · decision</h2>{error}{_action_html(run, busy)}</section>"
        f"<section><h2>Result · extraction</h2>{_extracted_html(run.get('extracted'))}</section>"
    )


def render_page(inbox: list[dict], runs: list[dict], selected_id: str | None,
                notice: str = "", runner: Runner | None = None,
                show_images: bool = False, origin: str = "") -> str:
    """`inbox`: live Gmail messages (merged with their run when processed).
    `runs`: processed messages that are no longer in the inbox list."""
    everything = inbox + runs
    notice_html = f'<p class="notice">{escape(notice)}</p>' if notice else ""
    if not everything:
        body = (f'<nav><h1>gmail-agent</h1>{notice_html}</nav><main><p class="empty">'
                "No emails to show.</p></main>")
    else:
        run = next((r for r in everything if r["message_id"] == selected_id), everything[0])
        nav = f"<nav><h1>gmail-agent</h1>{notice_html}"
        if inbox:
            nav += f"<h4>Inbox · live from Gmail ({len(inbox)})</h4>"
            nav += "".join(_nav_item(r, r is run, runner) for r in inbox)
        if runs:
            nav += f"<h4>Processed, no longer in this list ({len(runs)})</h4>"
            nav += "".join(_nav_item(r, r is run, runner) for r in runs)
        nav += "</nav>"
        body = (
            f"{nav}<main>"
            f"<section><h2>Input · email from Gmail</h2>{_email_html(run.get('email'), show_images, origin)}</section>"
            f"<div>{_results_html(run, runner)}</div></main>"
        )
    refresh = ('<meta http-equiv="refresh" content="3">'
               if runner is not None and runner.running else "")
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">{refresh}'
        f"<title>gmail-agent</title><style>{CSS}</style></head><body>{body}</body></html>"
    )


# --------------------------------------------------------------------------- #
# Server
# --------------------------------------------------------------------------- #
def serve(saver: SqliteSaver, client: GmailClient | None = None, port: int = 8000,
          query: str = "in:inbox", max_results: int = 25,
          store: ProcessedStore | None = None, run_client: GmailClient | None = None,
          attachments_only: bool = False) -> None:
    """`client` serves the page, `run_client` the pipeline runs. The Google API client is
    not thread-safe, so each gets its own connection and page requests share a lock."""
    cache: dict[str, Email] = {}
    gmail_lock = threading.RLock()
    runner = None
    if run_client is not None and store is not None:
        from gmail_agent.graph import build_graph

        runner = Runner(build_graph(run_client, checkpointer=saver), store)
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _email(message_id: str) -> Email:
        with gmail_lock:
            if message_id not in cache:
                cache[message_id] = client.get_email(message_id)
            return cache[message_id]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            url = urlparse(self.path)
            params = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path == "/attachment":
                self._attachment(params.get("id", ""), params.get("n", ""))
                return
            if url.path != "/":
                self.send_error(404)
                return
            selected = params.get("id")
            with gmail_lock:
                emails, notice = load_inbox(client, query, max_results, cache)
            runs = {r["message_id"]: r for r in load_runs(saver)}
            inbox = []
            for e in emails:
                run = runs.pop(e.id, None) or {"message_id": e.id, "waiting": False}
                run["email"] = e  # the live copy has the HTML body and fresh labels
                inbox.append(run)
            others = list(runs.values())
            if attachments_only:  # Gmail's has:attachment also matches inline-only images
                inbox = [r for r in inbox if _real_attachments(r["email"])]
                others = [r for r in others if r.get("email") and _real_attachments(r["email"])]
            # Processed emails that left the inbox: refresh the selected one from Gmail.
            for run in others:
                if run["message_id"] == selected and client is not None:
                    try:
                        run["email"] = _email(selected)
                    except Exception:
                        pass  # deleted or offline: keep the copy saved at processing time
            page = render_page(inbox, others, selected, notice, runner,
                               show_images=params.get("images") == "1",
                               origin=f"http://{self._host()}").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def _host(self) -> str:
            host = self.headers.get("Host")
            return host if host in allowed_hosts else f"127.0.0.1:{port}"

        def _attachment(self, message_id: str, index: str) -> None:
            if client is None or not message_id or not index.isdigit():
                self.send_error(404)
                return
            try:
                email = _email(message_id)
                att = email.attachments[int(index)]
                with gmail_lock:
                    data = client.download_attachment(message_id, att.attachment_id)
            except IndexError:
                self.send_error(404)
                return
            except Exception as exc:
                self.send_error(502, f"Gmail error: {exc.__class__.__name__}")
                return
            inline = att.mime_type in INLINE_TYPES
            disposition = "inline" if inline else "attachment"
            self.send_response(200)
            self.send_header("Content-Type", att.mime_type if inline else "application/octet-stream")
            self.send_header("Content-Disposition",
                             f"{disposition}; filename*=UTF-8''{quote(att.filename)}")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "private, max-age=3600")
            if att.mime_type != "application/pdf":  # Chrome's PDF viewer will not run sandboxed
                self.send_header("Content-Security-Policy", "sandbox")
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            # Buttons change the mailbox, so only accept them from this page: reject other
            # sites posting to localhost (Origin) and DNS-rebinding hostnames (Host).
            origin = urlparse(self.headers.get("Origin") or "").netloc
            if self.headers.get("Host") not in allowed_hosts or (origin and origin not in allowed_hosts):
                self.send_error(403)
                return
            if runner is None:
                self.send_error(503, "Gmail is not connected")
                return
            length = int(self.headers.get("Content-Length") or 0)
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
            mid = form.get("id", "")
            path = urlparse(self.path).path
            if not mid:
                self.send_error(400)
                return
            if path == "/process":
                runner.start(mid)
            elif path == "/approve":
                approved = form.get("decision") == "approve"
                runner.start(mid, {"approved": approved, "body": form.get("body", "")})
            elif path == "/note":
                runner.start(mid, form.get("note", "").strip() or "acknowledged")
            else:
                self.send_error(404)
                return
            self.send_response(303)  # back to the page, which then shows the progress
            self.send_header("Location", f"/?id={quote(mid)}")
            self.end_headers()

        def log_message(self, *args) -> None:  # keep the terminal quiet
            pass

    # localhost only: the page shows private mail
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"UI running at http://127.0.0.1:{port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
