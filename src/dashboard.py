"""Offline, read-only SignalDesk dashboard for fictional CSV records."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import socket
import sys
from email import policy
from email.parser import BytesParser
from collections import Counter
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from local_app import MAX_CSV_BYTES, SupabaseSession, process_csv
from signaldesk import api_request, normalize, read_csv, validate_supabase_url


ROOT = Path(__file__).resolve().parents[1]
DEMO_CSV = ROOT / "sample-data" / "demo-leads.csv"
CSS = Path(__file__).with_name("dashboard.css")
LOCAL_CSS = Path(__file__).with_name("local-dashboard.css")
SETUP_SQL = ROOT / "database" / "001_signaldesk.sql"
PROJECT_URL = "https://demo-project.supabase.co"  # Fictional test URL, not a real connection
TABLE_FIELDS = {
    "gtm_companies": "id,name,domain,hiring_signal,signal_source_url,qualification_status",
    "gtm_contacts": "id,company_id,full_name,job_title,contact_status",
    "gtm_outreach": "id,contact_id,delivery_status,sent_at",
    "gtm_import_log": "id,result",
}


def read_table(table, page_size=200, requester=None):
    if table not in TABLE_FIELDS or not 1 <= page_size <= 500:
        raise ValueError("Unsupported dashboard table")
    rows = []
    offset = 0
    requester = requester or api_request
    while True:
        path = f"{table}?select={TABLE_FIELDS[table]}&order=id.asc&limit={page_size}&offset={offset}"
        page = requester("GET", path)
        if not isinstance(page, list) or len(page) > page_size:
            raise RuntimeError("Invalid Supabase read response")
        rows.extend(page)
        if len(page) < page_size:
            return rows
        offset += page_size


def prepare_dashboard(csv_path=DEMO_CSV):
    """Derive display data from the same local validation as the importer."""
    leads, report = normalize(read_csv(csv_path))
    companies = {}
    for lead in leads:
        key = lead["company_domain"]
        company = companies.setdefault(key, {
            "name": lead["company_name"], "domain": key,
            "signal": lead["hiring_signal"], "source": lead["signal_source_url"],
            "contacts": 0,
        })
        company["contacts"] += 1
    drafts = sum(bool(lead[part]) for lead in leads for part in ("email_1_body", "email_2_body"))
    return {
        "source": "demo", "source_notice": "",
        "companies": list(companies.values()), "contacts": leads,
        "company_count": len(companies), "contact_count": len(leads),
        "signal_count": sum(bool(c["signal"]) for c in companies.values()),
        "draft_count": drafts, "statuses": dict(Counter(lead["qualification_status"] for lead in leads)),
        "report": report,
    }


def prepare_supabase_dashboard(requester=None):
    """Read a startup snapshot using server-only credentials and selected columns."""
    if requester is None:
        validate_supabase_url(os.getenv("SUPABASE_URL", ""))
        if not os.getenv("SUPABASE_SERVICE_ROLE_KEY"):
            raise ValueError("SUPABASE_SERVICE_ROLE_KEY is missing")
    companies = read_table("gtm_companies", requester=requester)
    contacts = read_table("gtm_contacts", requester=requester)
    outreach = read_table("gtm_outreach", requester=requester)
    imports = read_table("gtm_import_log", requester=requester)
    contact_counts = Counter(c["company_id"] for c in contacts)
    drafts_by_contact = Counter(o["contact_id"] for o in outreach if o["delivery_status"] == "draft")
    company_names = {c["id"]: c["name"] for c in companies}
    display_companies = [{"name": c["name"], "domain": c["domain"],
                          "signal": c.get("hiring_signal") or "", "source": c.get("signal_source_url") or "",
                          "contacts": contact_counts[c["id"]]} for c in companies]
    display_contacts = [{"full_name": c["full_name"], "job_title": c.get("job_title") or "",
                         "company_name": company_names.get(c["company_id"], "Unknown company"),
                         "qualification_status": c["contact_status"],
                         "draft_count": drafts_by_contact[c["id"]]} for c in contacts]
    results = Counter(row["result"] for row in imports)
    return {
        "source": "supabase", "source_notice": "",
        "companies": display_companies, "contacts": display_contacts,
        "company_count": len(companies), "contact_count": len(contacts),
        "signal_count": sum(bool(c["signal"]) for c in display_companies),
        "draft_count": sum(o["delivery_status"] == "draft" for o in outreach),
        "sent_count": sum(bool(o.get("sent_at")) for o in outreach),
        "statuses": dict(Counter(c["contact_status"] for c in contacts)),
        "import_metrics": (len(imports), results["NEW"],
                           sum(results[x] for x in ("EXISTING_RESEARCHED", "ALREADY_CONTACTED", "DO_NOT_CONTACT")),
                           results["INVALID_INPUT"]),
    }


def load_dashboard(source="auto", csv_path=DEMO_CSV):
    if source not in ("auto", "demo", "supabase"):
        raise ValueError("Invalid dashboard source")
    if source == "demo":
        return prepare_dashboard(csv_path)
    if source == "auto" and not (os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_ROLE_KEY")):
        data = prepare_dashboard(csv_path)
        if os.getenv("SUPABASE_URL") or os.getenv("SUPABASE_SERVICE_ROLE_KEY"):
            data["source_notice"] = "Incomplete Supabase configuration. Showing fictional demo data."
        return data
    try:
        return prepare_supabase_dashboard()
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):
        if source == "supabase":
            raise RuntimeError("Supabase read failed; check server-side configuration and table access") from None
        data = prepare_dashboard(csv_path)
        data["source_notice"] = "Live data unavailable. Showing fictional demo data."
        return data


def render_dashboard(data):
    e = lambda value: escape(str(value), quote=True)
    live = data["source"] == "supabase"
    mode_badge = "PRIVATE SUPABASE SNAPSHOT" if live else "FICTIONAL DEMO"
    mode_note = ("Private Supabase data. This is a read-only snapshot taken when the server started."
                 if live else "All names, companies, signals, and drafts shown here are fictional. No messages have been sent.")
    import_values = data["import_metrics"] if live else (
        data["report"]["input_rows"], data["report"]["accepted"],
        len(data["report"]["duplicates_in_csv"]), len(data["report"]["rejected"]))
    import_labels = ("Import events", "New", "Repeat detected", "Invalid") if live else (
        "CSV rows", "Accepted", "Duplicates skipped", "Rejected")
    company_rows = "".join(
        f'<tr><td><strong>{e(c["name"])}</strong><small>{e(c["domain"])}</small></td>'
        f'<td>{e(c["signal"] or ("No stored signal" if live else "No signal in demo CSV"))}</td>'
        f'<td>{e(c["contacts"])}</td></tr>' for c in data["companies"]
    )
    contact_rows = "".join(
        f'<tr><td><strong>{e(c["full_name"])}</strong><small>{e(c["job_title"] or "Role not listed")}</small></td>'
        f'<td>{e(c["company_name"])}</td><td><span class="pill">{e(c["qualification_status"].replace("_", " ").title())}</span></td>'
        f'<td>{e(c.get("draft_count", sum(bool(c.get(part)) for part in ("email_1_body", "email_2_body"))))}</td></tr>'
        for c in data["contacts"]
    )
    signals = "".join(
        f'<article class="signal"><span class="signal-mark">↗</span><div><strong>{e(c["name"])}</strong>'
        f'<p>{e(c["signal"])}</p><small>{"Source" if live else "Fictional source"}: {e(c["source"] or "Not provided")}</small></div></article>'
        for c in data["companies"] if c["signal"]
    ) or f'<p class="muted">{"No stored hiring signals." if live else "No hiring signals in this demo CSV."}</p>'
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>SignalDesk | {e(mode_badge.title())}</title><link rel="stylesheet" href="/dashboard.css"></head>
<body><div class="shell"><aside class="sidebar"><div class="brand"><span class="brand-icon">S</span><span>SignalDesk<small>DEMO WORKSPACE</small></span></div>
<nav aria-label="Dashboard"><a class="active" href="#overview">◫ &nbsp; Overview</a><a href="#companies">▦ &nbsp; Companies</a><a href="#contacts">♙ &nbsp; Contacts</a><a href="#campaign">✉ &nbsp; Campaign</a></nav>
<div class="sidebar-foot"><span class="dot"></span> {"Private local view" if live else "Offline demo"}<br><small>{"Supabase startup snapshot" if live else "Fictional sample data only"}</small></div></aside>
<main id="overview"><header class="topbar"><span>Workspace / Overview</span><span class="demo-badge">● &nbsp; {e(mode_badge)}</span></header>
<div class="content"><div class="heading"><div><p class="eyebrow">SIGNAL INTELLIGENCE</p><h1>Pipeline overview</h1><p class="subtitle">A clear view of local CSV research, contact review, and draft activity.</p></div><span class="read-only">Read-only preview</span></div>
<section class="notice"><strong>{"Private data." if live else "Demo data only."}</strong> {e(data["source_notice"] or mode_note)}</section>
<section class="metrics" aria-label="Summary metrics"><article class="metric"><span>Companies</span><strong>{e(data["company_count"])}</strong><small>{"In Supabase" if live else "In sample CSV"}</small></article><article class="metric"><span>Contacts</span><strong>{e(data["contact_count"])}</strong><small>{"Stored contacts" if live else "Unique accepted leads"}</small></article><article class="metric"><span>Hiring signals</span><strong>{e(data["signal_count"])}</strong><small>Company-level evidence</small></article><article class="metric"><span>Outreach drafts</span><strong>{e(data["draft_count"])}</strong><small>Unsent draft messages</small></article></section>
<div class="grid"><section class="panel" id="companies"><div class="panel-head"><div><p class="eyebrow">ACCOUNT RESEARCH</p><h2>Companies</h2></div><span class="count">{e(data["company_count"])} total</span></div><div class="table-wrap"><table><thead><tr><th>Company</th><th>Hiring evidence</th><th>Contacts</th></tr></thead><tbody>{company_rows}</tbody></table></div></section>
<section class="panel" id="campaign"><div class="panel-head"><div><p class="eyebrow">CAMPAIGN HEALTH</p><h2>Draft status</h2></div></div><div class="campaign-state"><span class="state-icon">✉</span><div><strong>{"Campaign snapshot" if live else "Drafts only"}</strong><p>{e(data["draft_count"])} {"stored" if live else "fictional"} message drafts prepared for review.</p></div></div><div class="stat-line"><span>Sent messages</span><strong>{e(data.get("sent_count", 0))}</strong></div><div class="stat-line"><span>{"Researched contacts" if live else "Contacts awaiting review"}</span><strong>{e(data["statuses"].get("researched" if live else "review", 0))}</strong></div><p class="fine-print">No outreach or campaign approval is performed by this dashboard.</p></section></div>
<div class="grid lower"><section class="panel" id="contacts"><div class="panel-head"><div><p class="eyebrow">PEOPLE</p><h2>Contacts</h2></div><span class="count">{e(data["contact_count"])} unique</span></div><div class="table-wrap"><table><thead><tr><th>Contact</th><th>Company</th><th>Status</th><th>Drafts</th></tr></thead><tbody>{contact_rows}</tbody></table></div></section>
<section class="panel"><div class="panel-head"><div><p class="eyebrow">IMPORT CHECK</p><h2>Duplicate prevention</h2></div></div><div class="import-metrics">{"".join(f'<div><strong>{e(value)}</strong><span>{e(label)}</span></div>' for value, label in zip(import_values, import_labels))}</div><p class="fine-print">{"Counts from stored import events; repeat events can include multiple imports of one contact." if live else "Local CSV validation only. No live database duplicate check is shown."}</p></section></div>
<section class="panel signals"><div class="panel-head"><div><p class="eyebrow">OBSERVED IN SAMPLE</p><h2>Hiring signals</h2></div></div>{signals}</section>
<footer>SignalDesk · {"Private Supabase snapshot" if live else "Fictional portfolio demo"} · Local and read-only</footer></div></main></div></body></html>'''


def render_local_page(server, view="dashboard", message="", error=False):
    """Reuse the existing dashboard shell; the published static demo stays intact."""
    e = lambda value: escape(str(value), quote=True)
    page = render_dashboard(server.dashboard_data)
    page = page.replace('</head>', '<link rel="stylesheet" href="/local-dashboard.css"></head>', 1)
    page = page.replace('DEMO WORKSPACE', 'GTM CREDIT SAVER', 1)
    page = page.replace('Workspace / Overview', f'Workspace / {e(view.title())}', 1)
    before, nav_start, remainder = page.partition('<nav aria-label="Dashboard">')
    _, nav_end, after = remainder.partition('</nav>')
    if not nav_start or not nav_end:
        raise RuntimeError("Dashboard shell is missing its navigation")
    options = ''.join(
        f'<option value="{name}"{" selected" if view == name else ""}>{label}</option>'
        for name, label in (("dashboard", "Dashboard"), ("upload", "Upload CSV"),
                            ("connect", "Connect Supabase"))
    )
    picker = ('<form class="view-picker" method="get" action="/">'
              '<label for="view">WORKSPACE</label><select id="view" name="view">'
              f'{options}</select><button type="submit">Open</button></form>')
    page = before + picker + after
    notice = (f'<div class="form-message {"error" if error else "success"}" role="status">'
              f'{e(message)}</div>') if message else ''

    if view == "dashboard":
        if server.lookup_stats:
            labels = (("total", "Prospects checked"), ("existing", "Existing contacts"),
                      ("recovered", "Saved emails recovered"), ("enrich", "Need enrichment"),
                      ("suppressed", "Suppressed"), ("review", "Manual review"))
            cards = ''.join(
                f'<article class="metric"><span>{label}</span><strong>{e(server.lookup_stats[key])}</strong></article>'
                for key, label in labels
            )
            extra = f'<section class="lookup-summary"><p class="eyebrow">LATEST CSV LOOKUP</p><div class="lookup-metrics">{cards}</div></section>'
        else:
            extra = '<section class="panel lookup-summary"><p class="eyebrow">CSV LOOKUP</p><p class="muted">Upload a CSV to see credit-saving statistics for this local session.</p></section>'
        return page.replace('<section class="metrics"', notice + extra + '<section class="metrics"', 1)

    connected = server.connection is not None
    status = ('<span class="connection-status connected">Connected</span>' if connected else
              '<span class="connection-status">Not Connected</span>')
    if view == "connect":
        content = f'''<div class="content"><div class="heading"><div><p class="eyebrow">FIRST-TIME SETUP</p><h1>Connect Supabase</h1><p class="subtitle">Use your own project for private, read-only CSV lookup.</p></div>{status}</div>{notice}
<section class="panel setup-panel"><h2>1. Create your project</h2><p>In Supabase, create a new project. Open its SQL Editor and run the <a href="/setup.sql">SignalDesk database setup SQL</a> in that project.</p>
<h2>2. Connect this local session</h2><p>Copy the Project URL and private service-role key from your Supabase project settings. The key stays in this Python process only; it is never saved to a file or browser storage.</p>
<form method="post" action="/connect" class="stacked-form" autocomplete="off"><input type="hidden" name="csrf_token" value="{e(server.csrf_token)}"><label>Project URL<input name="url" type="url" placeholder="https://your-project.supabase.co" required></label>
<label>Private service-role key<input name="key" type="password" autocomplete="new-password" required></label><button class="primary-button" type="submit">Test Connection</button></form>
<p class="fine-print">Use only your own Supabase project. Connection tests make GET requests and change no records. Credentials disappear when this server stops.</p></section>
<footer>SignalDesk · Private localhost workspace · Read-only lookup</footer></div>'''
    else:
        preview = ''
        if server.lookup_rows is not None:
            table_rows = ''.join(
                f'<tr><td>{e(row["SignalDesk Row"])}</td><td>{e(row.get("Full Name") or row.get("Name") or row.get("First Name") or "Prospect")}</td>'
                f'<td>{e(row["SignalDesk Status"])}</td><td>{e(row["SignalDesk Final Email"])}</td><td>{e(row["SignalDesk Enrich Email"])}</td></tr>'
                for row in server.lookup_rows[:100]
            )
            stats = server.lookup_stats
            preview = f'''<section class="panel lookup-results"><div class="panel-head"><div><p class="eyebrow">COMPLETED LOOKUP</p><h2>Results</h2></div><a class="primary-button" href="/download">Download Completed CSV</a></div>
<p class="muted">{e(stats["total"])} checked · {e(stats["existing"])} existing · {e(stats["recovered"])} emails recovered · {e(stats["enrich"])} need enrichment · {e(stats["suppressed"])} suppressed · {e(stats["review"])} review</p>
<div class="table-wrap"><table><thead><tr><th>Row</th><th>Prospect</th><th>Status</th><th>Final email</th><th>Enrich?</th></tr></thead><tbody>{table_rows}</tbody></table></div><p class="fine-print">Showing up to 100 rows here. The download includes every original row and column.</p></section>'''
        content = f'''<div class="content"><div class="heading"><div><p class="eyebrow">PRE-ENRICHMENT LOOKUP</p><h1>Upload CSV</h1><p class="subtitle">Check prospects against saved contacts before spending enrichment credits.</p></div>{status}</div>{notice}
<section class="panel setup-panel"><h2>Upload prospects</h2><p>Clay, Apollo, or other UTF-8 CSV files are supported. Include a LinkedIn profile or work email column. Every original row and column is preserved.</p>
<form method="post" action="/upload" enctype="multipart/form-data" class="stacked-form"><input type="hidden" name="csrf_token" value="{e(server.csrf_token)}"><label>Choose CSV<input type="file" name="file" accept=".csv,text/csv" required></label><button class="primary-button" type="submit"{" disabled" if not connected else ""}>Check Prospects</button></form>
<p class="fine-print">Maximum 5 MB or 10,000 rows. Connect Supabase first. No database writes, emails, or paid enrichments.</p></section>{preview}
<footer>SignalDesk · Private localhost workspace · Read-only lookup</footer></div>'''
    prefix, marker, _ = page.partition('<div class="content">')
    _, ending, suffix = page.rpartition('</div></main></div></body></html>')
    if not marker or not ending:
        raise RuntimeError("Dashboard shell is missing its content area")
    return prefix + content + ending[len('</div>'):] + suffix


class PrivateDashboardServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        # Paths and query strings are not logged from this private local server.
        pass

    def _host_ok(self):
        return self.headers.get("Host", "").lower() in {
            f"127.0.0.1:{self.server.server_port}",
            f"localhost:{self.server.server_port}",
        }

    def _origin_ok(self):
        return self.headers.get("Origin", "").lower() in {
            f"http://127.0.0.1:{self.server.server_port}",
            f"http://localhost:{self.server.server_port}",
        }

    def _get_origin_ok(self):
        # Browsers may omit Origin or send "null" for a top-level local
        # navigation. GET routes are read-only; POSTs still require _origin_ok.
        origin = self.headers.get("Origin")
        return origin is None or origin.lower() == "null" or self._origin_ok()

    def _post_origin_ok(self):
        origin = self.headers.get("Origin")
        return origin is None or origin.lower() == "null" or self._origin_ok()

    def _csrf_ok(self, raw, content_type):
        if content_type == "application/x-www-form-urlencoded":
            fields = parse_qs(raw.decode("utf-8"), max_num_fields=20)
            candidate = fields.get("csrf_token", [""])[0]
        elif content_type.startswith("multipart/form-data;"):
            mime = BytesParser(policy=policy.default).parsebytes(
                b"MIME-Version: 1.0\r\nContent-Type: " + content_type.encode("ascii") + b"\r\n\r\n" + raw
            )
            tokens = [part.get_payload(decode=True) for part in mime.iter_parts()
                      if part.get_param("name", header="content-disposition") == "csrf_token"]
            candidate = tokens[0].decode("utf-8") if len(tokens) == 1 and isinstance(tokens[0], bytes) else ""
        else:
            return False
        return hmac.compare_digest(candidate, self.server.csrf_token)

    def _reject(self, reason):
        # Keep diagnostics useful for localhost routing without ever logging
        # query strings, form bodies, cookies, or authorization headers.
        safe_host = self.headers.get("Host", "")[:255]
        safe_origin = self.headers.get("Origin", "")[:255]
        safe_path = urlsplit(self.path).path[:255]
        print(json.dumps({"event": "request_rejected", "reason": reason,
                          "host": safe_host, "path": safe_path,
                          "origin": safe_origin, "pid": os.getpid()}),
              file=sys.stderr, flush=True)
        self.send_error(403)

    def _send(self, body, content_type, status=200, download_name=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'none'; style-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        if download_name:
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.end_headers()
        self.wfile.write(body)

    def _page(self, view, message="", error=False, status=200):
        body = render_local_page(self.server, view, message, error).encode("utf-8")
        self._send(body, "text/html; charset=utf-8", status)

    def do_GET(self):
        if not self._host_ok() or not self._get_origin_ok():
            self._reject("host" if not self._host_ok() else "origin")
            return
        route = urlsplit(self.path)
        if route.path in ("/", "/dashboard", "/upload", "/connect"):
            view = (parse_qs(route.query).get("view", ["dashboard"])[0]
                    if route.path == "/" else route.path[1:])
            if view not in ("dashboard", "upload", "connect"):
                self.send_error(404)
            else:
                self._page(view)
        elif route.path == "/dashboard.css":
            self._send(CSS.read_bytes(), "text/css; charset=utf-8")
        elif route.path == "/local-dashboard.css":
            self._send(LOCAL_CSS.read_bytes(), "text/css; charset=utf-8")
        elif route.path == "/setup.sql":
            self._send(SETUP_SQL.read_bytes(), "text/plain; charset=utf-8", download_name="signaldesk-setup.sql")
        elif route.path == "/download" and self.server.completed_csv is not None:
            self._send(self.server.completed_csv, "text/csv; charset=utf-8", download_name="signaldesk-completed.csv")
        else:
            self.send_error(404)

    def do_POST(self):
        if not self._host_ok() or not self._post_origin_ok():
            self._reject("host" if not self._host_ok() else "origin")
            return
        route = urlsplit(self.path)
        view = "connect" if route.path == "/connect" else "upload" if route.path == "/upload" else None
        if view is None:
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            limit = 16_384 if view == "connect" else MAX_CSV_BYTES + 100_000
            if not 0 < length <= limit:
                raise ValueError("Request is empty or too large")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("Upload ended unexpectedly")
            if not self._csrf_ok(raw, self.headers.get("Content-Type", "").split(";", 1)[0]
                                 if not self.headers.get("Content-Type", "").startswith("multipart/form-data;")
                                 else self.headers.get("Content-Type", "")):
                self._reject("csrf")
                return
            if view == "connect":
                if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
                    raise ValueError("Invalid connection form")
                form = parse_qs(raw.decode("utf-8"), max_num_fields=3)
                session = SupabaseSession(form.get("url", [""])[0], form.get("key", [""])[0])
                session.test()
                data = prepare_supabase_dashboard(requester=session.request)
                self.server.connection = session
                self.server.dashboard_data = data
                self.server.completed_csv = None
                self.server.lookup_rows = None
                self.server.lookup_stats = None
                self._page("connect", "Connected. Read-only table access verified.")
            else:
                if self.server.connection is None:
                    raise ValueError("Connect Supabase before uploading a CSV")
                self.server.completed_csv = None
                self.server.lookup_rows = None
                self.server.lookup_stats = None
                content_type = self.headers.get("Content-Type", "")
                if not content_type.startswith("multipart/form-data;"):
                    raise ValueError("Upload a CSV using the file chooser")
                mime = BytesParser(policy=policy.default).parsebytes(
                    b"MIME-Version: 1.0\r\nContent-Type: " + content_type.encode("ascii") + b"\r\n\r\n" + raw
                )
                parts = [part for part in mime.iter_parts()
                         if part.get_param("name", header="content-disposition") == "file"]
                if len(parts) != 1:
                    raise ValueError("Choose exactly one CSV file")
                payload = parts[0].get_payload(decode=True)
                if not isinstance(payload, bytes):
                    raise ValueError("Choose a valid CSV file")
                completed, rows, stats = process_csv(payload, self.server.connection)
                self.server.completed_csv = completed
                self.server.lookup_rows = rows
                self.server.lookup_stats = stats
                self._page("upload", "Lookup complete. Download your completed CSV.")
        except ValueError as error:
            safe_prefixes = ("Choose ", "CSV ", "Enter ", "Supabase ", "Could not ",
                             "Upload ", "Request ", "Connect ", "Unexpected ")
            message = str(error)
            if not message.startswith(safe_prefixes):
                message = "The request could not be processed. Check the CSV and connection."
            self._page(view, message, True, 400)
        except (UnicodeDecodeError, UnicodeEncodeError):
            self._page(view, "The form encoding is invalid. Please try again.", True, 400)
        except (RuntimeError, KeyError, TypeError):
            self._page(view, "Supabase returned unexpected data. Check the database SQL setup.", True, 400)


def create_server(host="127.0.0.1", port=8765, csv_path=DEMO_CSV, source="auto"):
    if host != "127.0.0.1":
        raise ValueError("SignalDesk's private dashboard must bind to 127.0.0.1")
    data = load_dashboard(source, csv_path)
    server = PrivateDashboardServer((host, port), DashboardHandler)
    server.dashboard_data = data
    server.connection = None
    server.csrf_token = secrets.token_urlsafe(32)
    if data["source"] == "supabase":
        server.connection = SupabaseSession(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_ROLE_KEY"))
    server.completed_csv = None
    server.lookup_rows = None
    server.lookup_stats = None
    return server


def main():
    parser = argparse.ArgumentParser(description="Run the read-only SignalDesk dashboard locally")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--source", choices=("auto", "demo", "supabase"), default="auto")
    args = parser.parse_args()
    with create_server(port=args.port, source=args.source) as server:
        print(f"SignalDesk {server.dashboard_data['source']} dashboard: http://127.0.0.1:{server.server_port}/", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
