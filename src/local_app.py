"""Private localhost CSV lookup and session-only Supabase connection helpers."""

from __future__ import annotations

import csv
import io
import json
from collections import Counter
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from contact_lookup import ALIASES, EXTRA_COLUMNS, evaluate_rows, field
from signaldesk import email_norm, fetch_all_rows, linkedin_norm, validate_supabase_url

MAX_CSV_BYTES = 5 * 1024 * 1024
MAX_ROWS = 10_000
TABLES = ("gtm_companies", "gtm_contacts", "gtm_outreach", "gtm_import_log")


class SupabaseSession:
    def __init__(self, url, key):
        self.url = validate_supabase_url(url)
        if not key or not key.strip():
            raise ValueError("Enter a private Supabase service-role key")
        self._key = key.strip()

    def request(self, method, path):
        if method != "GET" or path.split("?", 1)[0] not in TABLES:
            raise ValueError("Only read-only SignalDesk table requests are allowed")
        req = Request(
            f"{self.url}/rest/v1/{path}", method="GET",
            headers={"apikey": self._key, "Authorization": f"Bearer {self._key}",
                     "Accept": "application/json"},
        )
        try:
            with urlopen(req, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            raise ValueError(f"Supabase returned HTTP {error.code}; check the project URL, key, and SQL setup") from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise ValueError("Could not read Supabase; check the connection and SQL setup") from None

    def test(self):
        for table in TABLES:
            rows = self.request("GET", f"{table}?select=id&limit=1")
            if not isinstance(rows, list):
                raise ValueError("Unexpected Supabase response; check the SQL setup")

    def lookup_tables(self):
        companies = fetch_all_rows("gtm_companies", "id,name,domain", requester=self.request)
        contacts = fetch_all_rows(
            "gtm_contacts",
            "id,company_id,work_email,linkedin_url,contact_status,job_title,email_verification",
            requester=self.request,
        )
        outreach = fetch_all_rows(
            "gtm_outreach", "id,contact_id,delivery_status", requester=self.request
        )
        return companies, contacts, outreach


def process_csv(contents, session):
    """Preserve source cells and return completed CSV, preview rows, and counts."""
    if not contents or len(contents) > MAX_CSV_BYTES:
        raise ValueError("Choose a nonempty CSV smaller than 5 MB")
    try:
        decoded = contents.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(decoded, newline=""), strict=True)
        fields = reader.fieldnames or []
        if not fields or len(fields) > 200 or any(not name.strip() for name in fields):
            raise ValueError("CSV needs 1–200 named columns")
        folded = [name.strip().casefold() for name in fields]
        if len(set(folded)) != len(fields):
            raise ValueError("CSV has duplicate column names")
        if any(name.casefold() in folded for name in EXTRA_COLUMNS):
            raise ValueError("Upload the original CSV without SignalDesk result columns")
        if not any(name.casefold() in folded for name in ALIASES["email"] + ALIASES["linkedin"]):
            raise ValueError("CSV needs a LinkedIn profile or work email column")
        rows = []
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError("CSV has a row with missing or extra cells")
            rows.append(row)
            if len(rows) > MAX_ROWS:
                raise ValueError("CSV exceeds the 10,000-row limit")
        if not rows:
            raise ValueError("CSV has no prospect rows")
    except UnicodeDecodeError:
        raise ValueError("Save the CSV as UTF-8 and upload it again") from None
    except csv.Error:
        raise ValueError("CSV is malformed; check quoting and delimiters") from None

    companies, contacts, outreach = session.lookup_tables()
    completed = evaluate_rows(rows, contacts, companies, outreach)
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields + EXTRA_COLUMNS)
    writer.writeheader()
    writer.writerows(completed)

    known_emails = {email_norm(c.get("work_email")) for c in contacts if c.get("work_email")}
    known_profiles = {linkedin_norm(c.get("linkedin_url")) for c in contacts if c.get("linkedin_url")}
    found = sum(
        (email_norm(field(row, ALIASES["email"])) in known_emails if field(row, ALIASES["email"]) else False)
        or (linkedin_norm(field(row, ALIASES["linkedin"])) in known_profiles if field(row, ALIASES["linkedin"]) else False)
        for row in rows
    )
    counts = Counter(row["SignalDesk Status"] for row in completed)
    stats = {
        "total": len(rows), "existing": found,
        "recovered": sum(
            bool(not field(original, ALIASES["email"]) and result["SignalDesk Saved Email"]
                 and result["SignalDesk Final Email"] == result["SignalDesk Saved Email"])
            for original, result in zip(rows, completed)
        ),
        "enrich": sum(row["SignalDesk Enrich Email"] == "YES" for row in completed),
        "suppressed": counts["ALREADY_CONTACTED"] + counts["DO_NOT_CONTACT"],
        "review": sum(count for status, count in counts.items() if status.startswith("REVIEW_")),
    }
    return output.getvalue().encode("utf-8-sig"), completed, stats
