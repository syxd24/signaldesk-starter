import importlib.util
import os
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "src"))
spec = importlib.util.spec_from_file_location("dashboard", ROOT / "src" / "dashboard.py")
dashboard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dashboard)


class DashboardTests(unittest.TestCase):
    def test_hosted_static_demo_contains_only_fictional_snapshot(self):
        html = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
        self.assertIn("FICTIONAL DEMO", html)
        self.assertIn("DemoSoft", html)
        self.assertNotIn("PRIVATE SUPABASE SNAPSHOT", html)
        self.assertNotIn("SUPABASE_SERVICE_ROLE_KEY", html)
        self.assertEqual((ROOT / "site" / "dashboard.css").read_bytes(),
                         (ROOT / "src" / "dashboard.css").read_bytes())

    def test_demo_data_is_prepared_from_csv(self):
        data = dashboard.prepare_dashboard()
        self.assertEqual((data["company_count"], data["contact_count"], data["signal_count"], data["draft_count"]), (1, 2, 1, 4))
        self.assertEqual(data["statuses"], {"review": 2})
        self.assertEqual(data["report"]["duplicates_in_csv"], [])

    def test_duplicate_report_uses_email_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "demo.csv"
            text = dashboard.DEMO_CSV.read_text(encoding="utf-8")
            path.write_text(text + text.splitlines()[1] + "\n", encoding="utf-8")
            data = dashboard.prepare_dashboard(path)
        self.assertEqual(data["contact_count"], 2)
        self.assertEqual(len(data["report"]["duplicates_in_csv"]), 1)

    def test_http_response_is_read_only_and_fictional(self):
        server = dashboard.create_server(port=0, source="demo")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urlopen(base + "/") as response:
                html = response.read().decode("utf-8")
                self.assertEqual(response.status, 200)
                self.assertIn("text/html", response.headers["Content-Type"])
                self.assertIn("FICTIONAL DEMO", html)
                self.assertIn("No messages have been sent", html)
                self.assertIn("Duplicate prevention", html)
            with urlopen(base + "/dashboard.css") as response:
                self.assertEqual(response.status, 200)
                self.assertIn("text/css", response.headers["Content-Type"])
            with urlopen(Request(base + "/connect", headers={"Host": f"localhost:{server.server_port}"})) as response:
                self.assertEqual(response.status, 200)
                self.assertIn("Connect Supabase", response.read().decode("utf-8"))
            with urlopen(Request(base + "/connect", headers={
                    "Host": f"127.0.0.1:{server.server_port}", "Origin": "null"})) as response:
                self.assertEqual(response.status, 200)
                self.assertIn("Connect Supabase", response.read().decode("utf-8"))
            with urlopen(base + "/?view=connect") as response:
                self.assertEqual(response.status, 200)
            with self.assertRaises(HTTPError) as missing:
                urlopen(base + "/api")
            self.assertEqual(missing.exception.code, 404)
            with self.assertRaises(HTTPError) as wrong_host:
                urlopen(Request(base + "/", headers={"Host": "untrusted.example"}))
            self.assertEqual(wrong_host.exception.code, 403)
            with self.assertRaises(HTTPError) as wrong_origin_get:
                urlopen(Request(base + "/connect", headers={
                    "Host": f"127.0.0.1:{server.server_port}",
                    "Origin": "https://untrusted.example"}))
            self.assertEqual(wrong_origin_get.exception.code, 403)
            with self.assertRaises(HTTPError) as null_origin_post:
                urlopen(Request(base + "/connect", data=b"url=x&key=y", headers={
                    "Host": f"127.0.0.1:{server.server_port}", "Origin": "null",
                    "Content-Type": "application/x-www-form-urlencoded"}))
            self.assertEqual(null_origin_post.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_port_cannot_be_shared_by_a_second_dashboard(self):
        server = dashboard.create_server(port=0, source="demo")
        try:
            with self.assertRaises(OSError):
                dashboard.create_server(port=server.server_port, source="demo")
        finally:
            server.server_close()

    def test_supabase_snapshot_uses_get_reads_and_correct_labels(self):
        rows = [
            [{"id": "company-1", "name": "Private Co", "domain": "private.example", "hiring_signal": "Hiring a manager", "signal_source_url": "https://example.org/job", "qualification_status": "qualified"}],
            [{"id": "contact-1", "company_id": "company-1", "full_name": "Private Person", "job_title": "Director", "contact_status": "researched"}],
            [{"contact_id": "contact-1", "delivery_status": "draft", "sent_at": None},
             {"contact_id": "contact-1", "delivery_status": "sent", "sent_at": "2026-01-01T00:00:00Z"}],
            [{"id": 1, "result": "NEW"}, {"id": 2, "result": "EXISTING_RESEARCHED"}],
        ]
        env = {"SUPABASE_URL": dashboard.PROJECT_URL, "SUPABASE_SERVICE_ROLE_KEY": "test-secret-never-render"}
        with patch.dict(os.environ, env), patch.object(dashboard, "api_request", side_effect=rows) as fetch:
            data = dashboard.load_dashboard("supabase")
        self.assertEqual([call.args[0] for call in fetch.call_args_list], ["GET"] * 4)
        self.assertEqual([call.args[1].split("?")[0] for call in fetch.call_args_list],
                         ["gtm_companies", "gtm_contacts", "gtm_outreach", "gtm_import_log"])
        self.assertEqual((data["company_count"], data["contact_count"], data["signal_count"], data["draft_count"], data["sent_count"]), (1, 1, 1, 1, 1))
        self.assertEqual(data["import_metrics"], (2, 1, 1, 0))
        html = dashboard.render_dashboard(data)
        self.assertIn("PRIVATE SUPABASE SNAPSHOT", html)
        self.assertIn("Private Person", html)
        self.assertNotIn("fictional message", html)
        self.assertNotIn("test-secret-never-render", html)

    def test_auto_falls_back_to_demo_on_read_failure(self):
        env = {"SUPABASE_URL": dashboard.PROJECT_URL, "SUPABASE_SERVICE_ROLE_KEY": "test-secret"}
        with patch.dict(os.environ, env), patch.object(dashboard, "api_request", side_effect=RuntimeError("hidden")):
            data = dashboard.load_dashboard("auto")
            with self.assertRaisesRegex(RuntimeError, "Supabase read failed"):
                dashboard.load_dashboard("supabase")
        self.assertEqual(data["source"], "demo")
        self.assertIn("Live data unavailable", dashboard.render_dashboard(data))

    def test_rejects_non_supabase_project_before_network(self):
        with patch.dict(os.environ, {"SUPABASE_URL": "https://localhost", "SUPABASE_SERVICE_ROLE_KEY": "test-secret"}):
            with patch.object(dashboard, "api_request") as fetch:
                with self.assertRaisesRegex(RuntimeError, "Supabase read failed"):
                    dashboard.load_dashboard("supabase")
                fetch.assert_not_called()

    def test_dashboard_reads_all_pages(self):
        pages = [[{"id": 1}, {"id": 2}], [{"id": 3}]]
        with patch.object(dashboard, "api_request", side_effect=pages) as request:
            rows = dashboard.read_table("gtm_import_log", page_size=2)
        self.assertEqual(len(rows), 3)
        self.assertIn("offset=2", request.call_args_list[1].args[1])


if __name__ == "__main__":
    unittest.main()
