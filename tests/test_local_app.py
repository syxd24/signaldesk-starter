"""End-to-end localhost workflow with fictional data and mocked Supabase GETs."""

import csv
import io
import json
import os
import re
import sys
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import dashboard
import local_app
import signaldesk

COMPANIES = [{"id": "company-1", "name": "Example Software", "domain": "examplesoftware.test",
              "hiring_signal": "Fictional opening", "signal_source_url": "https://example.test/jobs"}]
CONTACTS = [
    {"id": "p1", "company_id": "company-1", "full_name": "Morgan Example", "linkedin_url": "https://linkedin.com/in/morgan-demo", "work_email": "morgan@examplesoftware.test", "job_title": "VP Sales", "email_verification": "unknown", "contact_status": "researched"},
    {"id": "p2", "company_id": "company-1", "full_name": "Jordan Example", "linkedin_url": "https://linkedin.com/in/jordan-demo", "work_email": "", "job_title": "Manager", "email_verification": "unknown", "contact_status": "researched"},
    {"id": "p3", "company_id": "company-1", "full_name": "Casey Example", "linkedin_url": "https://linkedin.com/in/casey-demo", "work_email": "casey@examplesoftware.test", "job_title": "CRO", "email_verification": "unknown", "contact_status": "contacted"},
    {"id": "p4", "company_id": "company-1", "full_name": "Avery Example", "linkedin_url": "https://linkedin.com/in/avery-demo", "work_email": "avery@examplesoftware.test", "job_title": "Director", "email_verification": "unknown", "contact_status": "do_not_contact"},
]
OUTREACH = [{"id": "o1", "contact_id": "p3", "delivery_status": "sent", "sent_at": "2026-01-01T00:00:00Z"}]
HEADERS = ["Full Name", "LinkedIn Profile", "Company", "Company Domain", "Job Title"]
NAMES = ["Morgan Example", "Jordan Example", "Taylor Example", "Casey Example", "Avery Example"]
SLUGS = ["morgan-demo", "jordan-demo", "taylor-demo", "casey-demo", "avery-demo"]


def source_csv():
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=HEADERS)
    writer.writeheader()
    for name, slug in zip(NAMES, SLUGS):
        writer.writerow({"Full Name": name, "LinkedIn Profile": f"https://linkedin.com/in/{slug}",
                         "Company": "Example Software", "Company Domain": "examplesoftware.test",
                         "Job Title": "Research"})
    return output.getvalue().encode("utf-8")


def csrf_token(html):
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    if not match:
        raise AssertionError("Dashboard form did not include a CSRF token")
    return match.group(1)


def fake_supabase(request, timeout=20):
    assert request.get_method() == "GET", "Database writes are forbidden"
    route = urlsplit(request.full_url)
    table = route.path.rsplit("/", 1)[-1]
    assert table in local_app.TABLES
    rows = {"gtm_companies": COMPANIES, "gtm_contacts": CONTACTS,
            "gtm_outreach": OUTREACH, "gtm_import_log": []}[table]
    if "select=id&limit=1" in route.query:
        rows = [{"id": rows[0]["id"]}] if rows else []
    return io.BytesIO(json.dumps(rows).encode("utf-8"))


class LocalAppTests(unittest.TestCase):
    def test_upload_lookup_download_and_connection(self):
        server = dashboard.create_server(port=0, source="demo")
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f"http://127.0.0.1:{server.server_port}"
        secret = "fictional-test-only-key"
        try:
            with patch.object(local_app, "urlopen", side_effect=fake_supabase) as reads:
                with urlopen(base + "/") as response:
                    html = response.read().decode()
                self.assertIn('name="view"', html)
                self.assertIn('value="upload"', html)
                self.assertIn('value="connect"', html)
                with urlopen(base + "/?view=connect") as response:
                    setup = response.read().decode()
                self.assertIn("Not Connected", setup)
                self.assertIn("/setup.sql", setup)
                token = csrf_token(setup)
                with urlopen(base + "/setup.sql") as response:
                    self.assertIn(b"create table", response.read())

                form = urlencode({"url": "https://another-project.supabase.co", "key": secret,
                                  "csrf_token": token}).encode()
                req = Request(base + "/connect", data=form, method="POST",
                              headers={"Origin": "null", "Content-Type": "application/x-www-form-urlencoded"})
                with urlopen(req) as response:
                    connected = response.read().decode()
                self.assertIn("Connected. Read-only table access verified.", connected)
                self.assertNotIn(secret, connected)
                self.assertNotIn("service_role_key", connected)
                self.assertIsNotNone(server.connection)

                boundary = "signaldesk-test-boundary"
                uploaded = source_csv()
                body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"csrf_token\"\r\n\r\n{token}\r\n"
                        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"prospects.csv\"\r\n"
                        "Content-Type: text/csv\r\n\r\n").encode() + uploaded + f"\r\n--{boundary}--\r\n".encode()
                req = Request(base + "/upload", data=body, method="POST",
                              headers={"Origin": base, "Content-Type": f"multipart/form-data; boundary={boundary}"})
                with urlopen(req) as response:
                    result_page = response.read().decode()
                self.assertIn("Download Completed CSV", result_page)
                self.assertIn("EXISTING_EMAIL", result_page)
                self.assertIn("EXISTING_MISSING_EMAIL", result_page)
                self.assertIn("DO_NOT_CONTACT", result_page)
                self.assertNotIn(secret, result_page)
                with urlopen(base + "/download") as response:
                    self.assertIn("attachment", response.headers["Content-Disposition"])
                    downloaded = response.read().decode("utf-8-sig")
                self.assertNotIn(secret, downloaded)
                reader = csv.DictReader(io.StringIO(downloaded, newline=""))
                self.assertEqual(reader.fieldnames, HEADERS + local_app.EXTRA_COLUMNS)
                rows = list(reader)
                self.assertEqual(len(rows), 5)
                originals = list(csv.DictReader(io.StringIO(uploaded.decode(), newline="")))
                self.assertEqual([{field: row[field] for field in HEADERS} for row in rows], originals)
                self.assertEqual([row["SignalDesk Status"] for row in rows],
                                 ["EXISTING_EMAIL", "EXISTING_MISSING_EMAIL", "NEW_EMAIL_NEEDED",
                                  "ALREADY_CONTACTED", "DO_NOT_CONTACT"])
                self.assertEqual([row["SignalDesk Enrich Email"] for row in rows],
                                 ["NO", "YES", "YES", "NO", "NO"])
                self.assertEqual(rows[0]["SignalDesk Final Email"], "morgan@examplesoftware.test")
                self.assertTrue(all(not row["SignalDesk Final Email"] for row in rows[3:]))
                self.assertEqual(server.lookup_stats,
                                 {"total": 5, "existing": 4, "recovered": 1, "enrich": 2,
                                  "suppressed": 2, "review": 0})
                with urlopen(base + "/") as response:
                    dashboard_html = response.read().decode()
                self.assertIn("Saved emails recovered", dashboard_html)
                self.assertTrue(all(call.args[0].get_method() == "GET" for call in reads.call_args_list))
                invalid = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"csrf_token\"\r\n\r\n{token}\r\n"
                           f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"bad.csv\"\r\n"
                           "Content-Type: text/csv\r\n\r\nName\r\nOnly a name\r\n"
                           f"--{boundary}--\r\n").encode()
                req = Request(base + "/upload", data=invalid, method="POST",
                              headers={"Origin": base, "Content-Type": f"multipart/form-data; boundary={boundary}"})
                with self.assertRaises(HTTPError) as bad_upload:
                    urlopen(req)
                self.assertEqual(bad_upload.exception.code, 400)
                self.assertIn("LinkedIn profile or work email", bad_upload.exception.read().decode())
                with self.assertRaises(HTTPError) as stale_download:
                    urlopen(base + "/download")
                self.assertEqual(stale_download.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_invalid_connection_and_cross_origin_rejected(self):
        server = dashboard.create_server(port=0, source="demo")
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urlopen(base + "/connect") as response:
                token = csrf_token(response.read().decode())
            form = urlencode({"url": "http://blocked-example.supabase.co", "key": "fake",
                              "csrf_token": token}).encode()
            no_token = Request(base + "/connect", data=urlencode({"url": "http://blocked-example.supabase.co",
                              "key": "fake"}).encode(), method="POST",
                               headers={"Origin": "null", "Content-Type": "application/x-www-form-urlencoded"})
            with self.assertRaises(HTTPError) as missing_token:
                urlopen(no_token)
            self.assertEqual(missing_token.exception.code, 403)
            req = Request(base + "/connect", data=form, method="POST",
                          headers={"Origin": base, "Content-Type": "application/x-www-form-urlencoded"})
            with self.assertRaises(HTTPError) as blocked:
                urlopen(req)
            self.assertEqual(blocked.exception.code, 400)
            self.assertIsNone(server.connection)
            cross = Request(base + "/connect", data=form, method="POST",
                            headers={"Origin": "https://untrusted.example", "Content-Type": "application/x-www-form-urlencoded"})
            with self.assertRaises(HTTPError) as denied:
                urlopen(cross)
            self.assertEqual(denied.exception.code, 403)
            local_host = Request(base + "/connect", data=form, method="POST",
                                 headers={"Host": f"localhost:{server.server_port}",
                                          "Origin": f"http://localhost:{server.server_port}",
                                          "Content-Type": "application/x-www-form-urlencoded"})
            with self.assertRaises(HTTPError) as accepted_host:
                urlopen(local_host)
            self.assertEqual(accepted_host.exception.code, 400)
            self.assertIsNone(server.connection)
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_csv_validation_before_database_read(self):
        class NoRead:
            def lookup_tables(self):
                raise AssertionError("Database should not be read for invalid CSV")
        with self.assertRaisesRegex(ValueError, "duplicate column"):
            local_app.process_csv(b"Work Email,work email\na@example.test,b@example.test\n", NoRead())
        with self.assertRaisesRegex(ValueError, "LinkedIn profile or work email"):
            local_app.process_csv(b"Name,Company\nA,Example\n", NoRead())
        with self.assertRaisesRegex(ValueError, "5 MB"):
            local_app.process_csv(b"a" * (local_app.MAX_CSV_BYTES + 1), NoRead())
        with self.assertRaisesRegex(ValueError, "missing or extra cells"):
            local_app.process_csv(b"Work Email\na@example.test,extra\n", NoRead())

    def test_flexible_headers_and_conflicts_use_existing_lookup_engine(self):
        class FictionalSession:
            def lookup_tables(self):
                return COMPANIES, CONTACTS, OUTREACH
        source = ("Email Address,LinkedIn URL,Custom field\n"
                  "morgan@examplesoftware.test,https://linkedin.com/in/jordan-demo,first\n"
                  "morgan@examplesoftware.test,https://linkedin.com/in/jordan-demo,second\n"
                  "new@examplesoftware.test,,third\n").encode()
        completed, rows, stats = local_app.process_csv(source, FictionalSession())
        self.assertEqual([row["SignalDesk Status"] for row in rows],
                         ["REVIEW_IDENTITY_CONFLICT", "REVIEW_DUPLICATE_IN_CSV", "NEW_EMAIL_PRESENT"])
        self.assertEqual(stats["review"], 2)
        self.assertEqual([row["SignalDesk Enrich Email"] for row in rows], ["NO", "NO", "NO"])
        self.assertIn(b"Custom field", completed)

    def test_supabase_url_accepts_user_projects_and_optional_denylist(self):
        self.assertEqual(signaldesk.validate_supabase_url("https://another-project.supabase.co/"),
                         "https://another-project.supabase.co")
        with patch.dict(os.environ, {"SUPABASE_URL": "https://another-project.supabase.co",
                                     "SUPABASE_SERVICE_ROLE_KEY": "fictional-key"}):
            signaldesk.require_signaldesk_project()
        for url in ("http://another-project.supabase.co", "https://127.0.0.1",
                    "https://example.com"):
            with self.assertRaises(ValueError):
                signaldesk.validate_supabase_url(url)
        with patch.dict(os.environ, {"SIGNALDESK_BLOCKED_PROJECT_IDS": "blocked-example,other-project"}):
            with self.assertRaises(ValueError):
                signaldesk.validate_supabase_url("https://blocked-example.supabase.co")

    def test_bad_key_error_is_safe_and_no_remote_body_is_shown(self):
        session = local_app.SupabaseSession("https://another-project.supabase.co", "fictional-secret")
        error = HTTPError("https://another-project.supabase.co/rest/v1/gtm_contacts", 401,
                          "private response text", {}, io.BytesIO(b"private prospect data"))
        with patch.object(local_app, "urlopen", side_effect=error):
            with self.assertRaises(ValueError) as raised:
                session.test()
        self.assertIn("HTTP 401", str(raised.exception))
        self.assertNotIn("fictional-secret", str(raised.exception))
        self.assertNotIn("private prospect data", str(raised.exception))

    def test_remote_bind_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "127.0.0.1"):
            dashboard.create_server(host="0.0.0.0", port=0, source="demo")


if __name__ == "__main__":
    unittest.main()
