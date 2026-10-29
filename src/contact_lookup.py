"""Read-only pre-enrichment lookup: merge a Clay prospect list with saved contacts.

Exports *one row per original input row*. All original Clay columns are preserved.
No API writes, no emails sent, no output contact data printed to stdout.
The returned CSV is private and should stay in imports/, which Git ignores.
"""
from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path

from signaldesk import (
    EMAIL_RE, SUPPRESS_STATUSES, domain_norm, email_norm, fetch_all_rows,
    linkedin_norm, require_signaldesk_project,
)

EXTRA_COLUMNS = [
    "SignalDesk Row", "SignalDesk Match Key", "SignalDesk Status",
    "SignalDesk Saved Email", "SignalDesk Final Email",
    "SignalDesk Saved Job Title", "SignalDesk Saved Company",
    "SignalDesk Email Verification", "SignalDesk Enrich Email",
]

ALIASES = {
    "email": ("Work Email", "Email", "Email Address", "Business Email"),
    "linkedin": ("LinkedIn Profile", "LinkedIn URL", "LinkedIn Profile URL",
                 "Person LinkedIn URL", "Person LinkedIn Profile URL", "Linkedin"),
    "domain": ("Company Domain", "Domain", "Company Website", "Website"),
}


def field(row, names):
    # A normalised header lookup handles case, spaces and alternate Clay exports.
    keys = {str(k).strip().casefold(): v for k, v in row.items() if k is not None}
    for name in names:
        item = keys.get(name.casefold())
        if item is not None and str(item).strip():
            return str(item).strip()
    return ""


def evaluate_rows(rows, contacts, companies, outreach):
    """Return augmented rows in input order; never remove unmatched/review rows."""
    company_by_id = {c["id"]: c for c in companies}
    by_email, by_linkedin = {}, {}
    for person in contacts:
        e = email_norm(person.get("work_email"))
        li = linkedin_norm(person.get("linkedin_url"))
        if e:
            by_email.setdefault(e, []).append(person)
        if li:
            by_linkedin.setdefault(li, []).append(person)
    previously_sent = {x["contact_id"] for x in outreach if x.get("delivery_status") in ("sent", "replied")}
    seen_emails, seen_linkedin = set(), set()
    output = []

    for number, source in enumerate(rows, 1):
        row = dict(source)
        raw_email = field(source, ALIASES["email"])
        raw_li = field(source, ALIASES["linkedin"])
        raw_domain = field(source, ALIASES["domain"])
        email = email_norm(raw_email)
        linkedin = linkedin_norm(raw_li)
        domain = domain_norm(raw_domain) if raw_domain else ""
        key = linkedin or email

        matching = {
            p["id"]: p for p in
            (by_email.get(email, []) if email else []) +
            (by_linkedin.get(linkedin, []) if linkedin else [])
        }
        match = next(iter(matching.values())) if len(matching) == 1 else None
        saved_email, final_email, saved_title, saved_company, verification = "", "", "", "", ""
        should_enrich = "NO"

        if raw_email and not EMAIL_RE.fullmatch(email):
            status = "REVIEW_INVALID_EMAIL"
        elif raw_li and not linkedin:
            status = "REVIEW_INVALID_LINKEDIN"
        elif not email and not linkedin:
            status = "REVIEW_NO_IDENTIFIER"
        elif (email and email in seen_emails) or (linkedin and linkedin in seen_linkedin):
            status = "REVIEW_DUPLICATE_IN_CSV"
        elif len(matching) > 1:
            status = "REVIEW_IDENTITY_CONFLICT"
        elif match:
            existing_email = email_norm(match.get("work_email"))
            existing_li = linkedin_norm(match.get("linkedin_url"))
            company = company_by_id.get(match.get("company_id"), {})
            saved_company = company.get("name") or ""
            saved_title = match.get("job_title") or ""
            verification = match.get("email_verification") or "unknown"
            if ((email and existing_email and email != existing_email)
                    or (linkedin and existing_li and linkedin != existing_li)):
                status = "REVIEW_IDENTITY_CONFLICT"
            elif domain and company.get("domain") and domain != company["domain"]:
                status = "REVIEW_COMPANY_CHANGED"
            elif match.get("contact_status") == "do_not_contact":
                status = "DO_NOT_CONTACT"
            elif (match.get("contact_status") in SUPPRESS_STATUSES
                  or match["id"] in previously_sent):
                status = "ALREADY_CONTACTED"
            else:
                saved_email = existing_email
                final_email = email or existing_email
                status = "EXISTING_EMAIL" if final_email else "EXISTING_MISSING_EMAIL"
                should_enrich = "NO" if final_email else "YES"
        else:
            final_email = email
            status = "NEW_EMAIL_PRESENT" if email else "NEW_EMAIL_NEEDED"
            should_enrich = "NO" if email else "YES"

        if email:
            seen_emails.add(email)
        if linkedin:
            seen_linkedin.add(linkedin)
        row.update({
            "SignalDesk Row": str(number),
            "SignalDesk Match Key": key,
            "SignalDesk Status": status,
            "SignalDesk Saved Email": saved_email,
            "SignalDesk Final Email": final_email,
            "SignalDesk Saved Job Title": saved_title,
            "SignalDesk Saved Company": saved_company,
            "SignalDesk Email Verification": verification,
            "SignalDesk Enrich Email": should_enrich,
        })
        output.append(row)
    return output


def run_lookup(input_path, output_path):
    require_signaldesk_project()
    src, target = Path(input_path).resolve(), Path(output_path).resolve()
    if src == target:
        raise ValueError("Lookup output must not overwrite the source CSV")
    with src.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        original_fields = reader.fieldnames or []
        if not original_fields:
            raise ValueError("Input CSV needs a header row")
        if len({x.strip().casefold() for x in original_fields}) != len(original_fields):
            raise ValueError("Duplicate column names in source CSV")
        if any(x in original_fields for x in EXTRA_COLUMNS):
            raise ValueError("CSV already includes SignalDesk output columns; use the original Clay export")
        rows = list(reader)

    # Read-only GET calls. Never send an RPC or POST and never modify Supabase.
    companies = fetch_all_rows("gtm_companies", "id,name,domain")
    contacts = fetch_all_rows(
        "gtm_contacts", "id,company_id,work_email,linkedin_url,contact_status,job_title,email_verification"
    )
    outreach = fetch_all_rows("gtm_outreach", "id,contact_id,delivery_status")
    results = evaluate_rows(rows, contacts, companies, outreach)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=original_fields + EXTRA_COLUMNS)
        writer.writeheader()
        writer.writerows(results)
    counts = dict(Counter(row["SignalDesk Status"] for row in results))
    # No private person, email, or source row values are written to stdout.
    print(json.dumps({
        "mode": "READ_ONLY", "input_rows": len(rows), "output_rows": len(results),
        "needs_email_enrichment": sum(row["SignalDesk Enrich Email"] == "YES" for row in results),
        "status_counts": counts, "private_output": str(target),
    }, indent=2))
    return 0
