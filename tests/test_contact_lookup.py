"""Read-only pre-enrichment lookup tests using fictional identities only."""
import csv
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import signaldesk
import contact_lookup as lookup


COMPANIES = [
    {"id": "c1", "domain": "examplesoftware.test", "name": "Example Software"}
]
CONTACTS = [
    {"id": "a", "company_id": "c1", "full_name": "Morgan Example",
     "linkedin_url": "https://linkedin.com/in/morgan-demo", "work_email": "morgan@examplesoftware.test",
     "job_title": "VP Sales", "email_verification": "unknown", "contact_status": "researched"},
    {"id": "b", "company_id": "c1", "full_name": "Jordan Example",
     "linkedin_url": "https://linkedin.com/in/jordan-demo", "work_email": "",
     "job_title": "Finance Manager", "email_verification": "unknown", "contact_status": "researched"},
    {"id": "c", "company_id": "c1", "full_name": "Blocked Example",
     "linkedin_url": "https://linkedin.com/in/blocked-demo", "work_email": "blocked@examplesoftware.test",
     "job_title": "Director", "email_verification": "verified", "contact_status": "do_not_contact"},
    {"id": "d", "company_id": "c1", "full_name": "Contacted Example",
     "linkedin_url": "https://linkedin.com/in/contacted-demo", "work_email": "contacted@examplesoftware.test",
     "job_title": "CRO", "email_verification": "unknown", "contact_status": "researched"},
]


def lead(name, slug, **extra):
    return {
        "Full Name": name,
        "LinkedIn Profile": f"https://www.linkedin.com/in/{slug}/?trk=test",
        "Company": "Example Software",
        "Company Domain": "examplesoftware.test",
        "Job Title": "Sales",
        **extra,
    }


class LookupTests(unittest.TestCase):
    def test_one_output_row_per_input_and_reused_email(self):
        rows = [
            lead("Morgan", "morgan-demo"), lead("Jordan", "jordan-demo"),
            lead("New Person", "new-demo"), lead("Blocked", "blocked-demo"),
            lead("Contacted", "contacted-demo"),
        ]
        original_rows = [dict(row) for row in rows]
        result = lookup.evaluate_rows(
            rows, CONTACTS, COMPANIES, [{"contact_id": "d", "delivery_status": "sent"}]
        )
        self.assertEqual(len(result), len(rows))
        self.assertEqual(rows, original_rows)
        self.assertEqual([r["SignalDesk Status"] for r in result], [
            "EXISTING_EMAIL", "EXISTING_MISSING_EMAIL", "NEW_EMAIL_NEEDED",
            "DO_NOT_CONTACT", "ALREADY_CONTACTED",
        ])
        self.assertEqual([r["SignalDesk Enrich Email"] for r in result], ["NO","YES","YES","NO","NO"])
        self.assertEqual(result[0]["SignalDesk Final Email"], "morgan@examplesoftware.test")
        self.assertEqual(result[0]["SignalDesk Saved Job Title"], "VP Sales")
        self.assertEqual(result[1]["SignalDesk Final Email"], "")
        self.assertEqual(result[3]["SignalDesk Saved Email"], "")
        self.assertEqual(result[3]["SignalDesk Final Email"], "")
        self.assertEqual(result[4]["SignalDesk Final Email"], "")
        self.assertEqual([r["SignalDesk Row"] for r in result], ["1","2","3","4","5"])

    def test_no_identifier_and_bad_linkedin_are_review_not_enriched(self):
        rows = [
            {"Full Name": "No ID", "Company": "Example Software"},
            {"Full Name": "Wrong Profile", "LinkedIn URL": "https://evil.test/in/fake"},
        ]
        result = lookup.evaluate_rows(rows, CONTACTS, COMPANIES, [])
        self.assertEqual([x["SignalDesk Status"] for x in result],
                         ["REVIEW_NO_IDENTIFIER", "REVIEW_INVALID_LINKEDIN"])
        self.assertTrue(all(x["SignalDesk Enrich Email"] == "NO" for x in result))

    def test_in_list_duplicate_kept_but_blocked(self):
        rows = [lead("Morgan", "morgan-demo"), lead("Morgan repeat", "morgan-demo")]
        out = lookup.evaluate_rows(rows, CONTACTS, COMPANIES, [])
        self.assertEqual(len(out), 2)
        self.assertEqual(out[1]["SignalDesk Status"], "REVIEW_DUPLICATE_IN_CSV")
        self.assertEqual(out[1]["SignalDesk Enrich Email"], "NO")

    def test_different_company_requires_review(self):
        row = lead("Morgan", "morgan-demo", **{"Company Domain": "anothercompany.test"})
        out = lookup.evaluate_rows([row], CONTACTS, COMPANIES, [])
        self.assertEqual(out[0]["SignalDesk Status"], "REVIEW_COMPANY_CHANGED")

    def test_conflicting_email_and_profile_stopped(self):
        row = lead("Morgan", "morgan-demo", **{"Work Email": "different@examplesoftware.test"})
        out = lookup.evaluate_rows([row], CONTACTS, COMPANIES, [])
        self.assertEqual(out[0]["SignalDesk Status"], "REVIEW_IDENTITY_CONFLICT")
        self.assertEqual(out[0]["SignalDesk Enrich Email"], "NO")

    def test_email_matches_one_contact_and_linkedin_another(self):
        row = lead("Mixed", "jordan-demo", **{"Work Email": "morgan@examplesoftware.test"})
        out = lookup.evaluate_rows([row], CONTACTS, COMPANIES, [])
        self.assertEqual(out[0]["SignalDesk Status"], "REVIEW_IDENTITY_CONFLICT")

    def test_missing_saved_email_but_source_email_kept(self):
        row = lead("Jordan", "jordan-demo", **{"Work Email": "jordan@examplesoftware.test"})
        out = lookup.evaluate_rows([row], CONTACTS, COMPANIES, [])
        self.assertEqual(out[0]["SignalDesk Final Email"], "jordan@examplesoftware.test")
        self.assertEqual(out[0]["SignalDesk Enrich Email"], "NO")

    def test_csv_keeps_all_original_columns_and_no_database_writes(self):
        originals = [
            lead("Morgan", "morgan-demo", **{"Custom campaign column": "A"}),
            lead("Unknown", "new-demo", **{"Custom campaign column": "B"}),
        ]
        with tempfile.TemporaryDirectory() as dirname:
            inp = Path(dirname) / "start.csv"
            out = Path(dirname) / "results.csv"
            with inp.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(originals[0]))
                w.writeheader()
                w.writerows(originals)
            printout = io.StringIO()
            with patch.dict(os.environ, {"SUPABASE_URL": "https://demo-project.supabase.co",
                                         "SUPABASE_SERVICE_ROLE_KEY": "fake-private-key"}), \
                 patch.object(lookup, "fetch_all_rows", side_effect=[COMPANIES, CONTACTS, []]) as fetch, \
                 patch.object(signaldesk, "api_request", side_effect=AssertionError("No network writes")), \
                 redirect_stdout(printout):
                self.assertEqual(lookup.run_lookup(inp, out), 0)
            self.assertEqual(fetch.call_count, 3)
            with out.open("r", newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                self.assertEqual(reader.fieldnames, list(originals[0]) + lookup.EXTRA_COLUMNS)
                rows = list(reader)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["Custom campaign column"], "A")
            self.assertEqual(rows[1]["Custom campaign column"], "B")
            self.assertEqual(rows[0]["SignalDesk Final Email"], "morgan@examplesoftware.test")
            report = json.loads(printout.getvalue())
            self.assertEqual(report["needs_email_enrichment"], 1)
            self.assertEqual(report["mode"], "READ_ONLY")
            self.assertNotIn("morgan@examplesoftware.test", printout.getvalue())
            self.assertNotIn("fake-private-key", printout.getvalue())

    def test_refuses_to_overwrite_source(self):
        with patch.dict(os.environ, {"SUPABASE_URL": "https://demo-project.supabase.co",
                                     "SUPABASE_SERVICE_ROLE_KEY": "fake-key"}):
            with self.assertRaisesRegex(ValueError, "must not overwrite"):
                lookup.run_lookup("sample.csv", "sample.csv")

    def test_cli_wiring(self):
        with patch.object(lookup, "run_lookup", return_value=0) as fn:
            self.assertEqual(signaldesk.main(["lookup", "foo.csv", "--output", "bar.csv"]), 0)
        fn.assert_called_once_with("foo.csv", "bar.csv")


if __name__ == "__main__":
    unittest.main()
