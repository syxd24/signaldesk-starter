"""SignalDesk: Clay CSV normalization, Supabase ingestion and suppression-list export.

Python stdlib only. No data is sent unless --confirm-upload is explicitly provided.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
PERSONAL_EMAIL_DOMAINS = {'gmail.com', 'outlook.com', 'hotmail.com', 'yahoo.com', 'icloud.com', 'proton.me', 'protonmail.com'}
SUPPRESS_STATUSES = {'contacted', 'replied', 'meeting_booked', 'deal_won', 'deal_lost', 'bounced', 'do_not_contact'}
# Optional private-installation safeguard: comma-separated Supabase project refs to refuse.
# Public starters do not ship any personal project identifiers.
BLOCKED_PROJECTS_ENV = 'SIGNALDESK_BLOCKED_PROJECT_IDS'


def validate_supabase_url(url):
    """Accept a standard Supabase project URL, never a local or unrelated host."""
    parsed = urlparse((url or '').strip())
    host = parsed.hostname or ''
    blocked = {x.strip().lower() for x in os.getenv(BLOCKED_PROJECTS_ENV, '').split(',') if x.strip()}
    if (parsed.scheme != 'https' or parsed.port is not None or parsed.username
            or parsed.password or parsed.path not in ('', '/') or parsed.query
            or parsed.fragment or not re.fullmatch(r'[a-z0-9-]+[.]supabase[.]co', host)
            or host.split('.')[0] in blocked):
        raise ValueError('Enter your own https://PROJECT.supabase.co URL')
    return f'https://{host}'


def value(row, *keys):
    for key in keys:
        v = row.get(key)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ''


def email_norm(email):
    return (email or '').strip().lower()


def linkedin_norm(url):
    if not url:
        return ''
    u = urlparse(url.strip())
    if not u.scheme:
        u = urlparse('https://' + url.strip())
    if not u.hostname or not re.fullmatch(r'(www\.)?linkedin\.com', u.hostname.lower()):
        return ''
    path = '/' + u.path.strip('/').lower()
    if not path.startswith('/in/') or len(path) < 6:
        return ''
    return 'https://www.linkedin.com' + path


def domain_norm(domain):
    s = (domain or '').strip().lower()
    if not s:
        return ''
    if '://' not in s:
        s = 'https://' + s
    u = urlparse(s)
    h = (u.hostname or '').lower().removeprefix('www.')
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}', h):
        return ''
    return h


def company_info(rows):
    # Preserve the best available original company-level evidence from the CSV.
    info = {}
    for row in rows:
        name = value(row, 'Company', 'Company Name')
        key = name.casefold()
        if not key:
            continue
        obj = info.setdefault(key, {'hiring_signal': '', 'signal_source_url': ''})
        if not obj['hiring_signal']:
            obj['hiring_signal'] = value(row, 'Use AI Hiring Signal', 'Email Hook', 'Hiring Observation')
        if not obj['signal_source_url']:
            obj['signal_source_url'] = value(row, 'Use AI Source Url', 'Job LinkedIn URL')
    for row in rows:
        name = value(row, 'Company', 'Company Name')
        key = name.casefold()
        if not key:
            continue
        obj = info[key]
        if not obj['hiring_signal']:
            body = value(row, 'Template Email 1')
            m = re.search(r'\bis\s+(hiring\s+[^\n.]+)', body, flags=re.IGNORECASE)
            if m:
                obj['hiring_signal'] = m.group(1).strip()
    return info


def normalize(rows, *, campaign='Sales Compensation Outreach', qualification='review'):
    if qualification not in {'review', 'qualified', 'exclude'}:
        raise ValueError('Invalid qualification status')
    company_data = company_info(rows)
    output = []
    rejected = []
    duplicates = []
    seen_email, seen_li = {}, {}
    for index, r in enumerate(rows, start=2):
        name = value(r, 'Full Name', 'Name')
        company = value(r, 'Company', 'Company Name')
        email = email_norm(value(r, 'Work Email', 'Email'))
        li_raw = value(r, 'LinkedIn Profile', 'LinkedIn URL')
        li = linkedin_norm(li_raw)
        explicit_domain = value(r, 'Company Domain', 'Domain', 'Company Website')
        company_domain = domain_norm(explicit_domain)
        derived = False
        if not company_domain and EMAIL_RE.fullmatch(email):
            inferred = domain_norm(email.split('@', 1)[1])
            if inferred not in PERSONAL_EMAIL_DOMAINS:
                company_domain = inferred
                derived = True
        issues = []
        if not company: issues.append('missing company')
        if not name: issues.append('missing full name')
        if not company_domain: issues.append('missing or invalid company domain')
        if email and not EMAIL_RE.fullmatch(email): issues.append('invalid email format')
        if li_raw and not li: issues.append('invalid LinkedIn profile URL')
        if not email and not li: issues.append('missing both email and LinkedIn')
        if issues:
            rejected.append({'row': index, 'reasons': issues})
            continue
        # One email or LinkedIn identity can belong to only one person in a batch.
        # Repeated identifiers with contradictory second identifiers are rejected,
        # rather than silently treating two different people as one person.
        by_email = seen_email.get(email) if email else None
        by_linkedin = seen_li.get(li) if li else None
        known = by_email if by_email is not None else by_linkedin
        conflicts = []
        if by_email is not None and by_linkedin is not None and by_email is not by_linkedin:
            conflicts.append('email and LinkedIn identify different rows')
        if by_email is not None and li and by_email['linkedin_url'] and by_email['linkedin_url'] != li:
            conflicts.append('same email has different LinkedIn profiles')
        if by_linkedin is not None and email and by_linkedin['work_email'] and by_linkedin['work_email'] != email:
            conflicts.append('same LinkedIn has different work emails')
        if known is not None and known['company_domain'] != company_domain:
            conflicts.append('same identity associated with different company domains')
        if conflicts:
            rejected.append({'row': index, 'reasons': conflicts})
            continue
        if known is not None:
            # A repeat may supply an identifier the first occurrence was missing.
            # Add the identifier to the original record without creating a new lead.
            if email and not known['work_email']:
                known['work_email'] = email
            if li and not known['linkedin_url']:
                known['linkedin_url'] = li
            if email: seen_email[email] = known
            if li: seen_li[li] = known
            duplicates.append({'row': index, 'reason': 'same contact already appears in this CSV'})
            continue
        ci = company_data.get(company.casefold(), {})
        lead = {
            'source': 'clay_csv',
            'company_name': company,
            'company_domain': company_domain,
            'full_name': name,
            'job_title': value(r, 'Job Title'),
            'linkedin_url': li,
            'work_email': email,
            # Found in Clay is not proof of independent email verification.
            'email_verification': 'unknown',
            'qualification_status': qualification,
            'hiring_signal': ci.get('hiring_signal', ''),
            'signal_source_url': ci.get('signal_source_url', ''),
            'campaign_name': campaign,
            'email_1_subject': value(r, 'Template Subject'),
            'email_1_body': value(r, 'Template Email 1'),
            'email_2_body': value(r, 'Template Follow Up'),
            '_company_domain_from_email': derived,
        }
        output.append(lead)
        if email: seen_email[email] = lead
        if li: seen_li[li] = lead
    report = {'input_rows': len(rows), 'accepted': len(output), 'rejected': rejected,
              'duplicates_in_csv': duplicates,
              'domain_inferred_from_email': sum(p['_company_domain_from_email'] for p in output)}
    # Private audit metadata isn't part of the API payload.
    for p in output: p.pop('_company_domain_from_email', None)
    return output, report


def read_csv(path):
    with open(path, 'r', encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def write_jsonl(path, records):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open('w', encoding='utf-8') as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')


def read_jsonl(path):
    with open(path, encoding='utf-8') as f:
        return [json.loads(x) for x in f if x.strip()]


def api_request(method, path, payload=None):
    url = validate_supabase_url(os.getenv('SUPABASE_URL', ''))
    key = os.getenv('SUPABASE_SERVICE_ROLE_KEY', '')
    if not key:
        raise RuntimeError('Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in the environment; never in the repo')
    data = None if payload is None else json.dumps(payload).encode('utf-8')
    try:
        req = Request(f'{url}/rest/v1/{path}', data=data, method=method,
                      headers={'apikey': key, 'Authorization': f'Bearer {key}',
                               'Content-Type': 'application/json', 'Accept': 'application/json'})
        with urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode('utf-8'))
    except HTTPError as e:
        # API errors can echo submitted contact payloads; never log remote response bodies.
        raise RuntimeError(f'Supabase HTTP {e.code}; check the request and Supabase logs privately') from e
    except URLError as e:
        raise RuntimeError('Network connection to Supabase failed') from e
    except ValueError as e:
        raise RuntimeError('Invalid Supabase request configuration') from e


def upload_payloads(payloads, *, confirmed=False):
    if not confirmed:
        raise RuntimeError('Refusing live upload without --apply')
    counts = Counter()
    for p in payloads:
        result = api_request('POST', 'rpc/gtm_ingest_lead', {'payload': p})
        outcome = result.get('result', 'UNKNOWN')
        counts[outcome] += 1
        # Do not print personal contact details in terminal logs.
    return dict(counts)


def require_signaldesk_project():
    try:
        validate_supabase_url(os.getenv('SUPABASE_URL', ''))
    except ValueError:
        raise RuntimeError('SUPABASE_URL must point to your Supabase project') from None
    if not os.getenv('SUPABASE_SERVICE_ROLE_KEY'):
        raise RuntimeError('Set SUPABASE_SERVICE_ROLE_KEY in the private server environment')


def fetch_all_rows(table, fields, *, page_size=200, requester=None):
    """Fetch every row in deterministic pages; never trust a single API page.

    page_size intentionally stays under Supabase's default 1000-row API limit.
    If the server's API maximum is configured below 200, lower page_size too.
    """
    if table not in {'gtm_companies', 'gtm_contacts', 'gtm_outreach'} or page_size < 1 or page_size > 500:
        raise ValueError('Unsupported table or page size')
    rows = []
    offset = 0
    requester = requester or api_request
    while True:
        chunk = requester('GET', f'{table}?select={fields}&order=id.asc&limit={page_size}&offset={offset}')
        if not isinstance(chunk, list):
            raise RuntimeError('Expected a list of rows from Supabase')
        if len(chunk) > page_size:
            raise RuntimeError('Supabase returned more rows than requested')
        rows.extend(chunk)
        if len(chunk) < page_size:
            return rows
        offset += page_size


def classify_import(payloads, companies, contacts, outreach):
    """Preview RPC outcomes without writing; contradictory identities need manual review."""
    domains = {c['id']: c['domain'] for c in companies}
    by_email, by_linkedin = {}, {}
    for contact in contacts:
        email = email_norm(contact.get('work_email'))
        linkedin = linkedin_norm(contact.get('linkedin_url'))
        if email:
            by_email.setdefault(email, []).append(contact)
        if linkedin:
            by_linkedin.setdefault(linkedin, []).append(contact)
    sent_ids = {row['contact_id'] for row in outreach if row.get('delivery_status') in {'sent', 'replied'}}
    decisions = []
    for index, lead in enumerate(payloads, start=1):
        email = email_norm(lead.get('work_email'))
        linkedin = linkedin_norm(lead.get('linkedin_url'))
        matches = {c['id']: c for c in by_email.get(email, []) + by_linkedin.get(linkedin, [])}
        if len(matches) > 1:
            result = 'CONFLICT_REVIEW'
        elif not matches:
            result = 'NEW'
        else:
            contact = next(iter(matches.values()))
            existing_email = email_norm(contact.get('work_email'))
            existing_linkedin = linkedin_norm(contact.get('linkedin_url'))
            if ((email and existing_email and email != existing_email)
                    or (linkedin and existing_linkedin and linkedin != existing_linkedin)
                    or domains.get(contact.get('company_id')) != lead['company_domain']):
                result = 'CONFLICT_REVIEW'
            elif contact.get('contact_status') == 'do_not_contact':
                result = 'DO_NOT_CONTACT'
            elif contact.get('contact_status') in SUPPRESS_STATUSES or contact['id'] in sent_ids:
                result = 'ALREADY_CONTACTED'
            else:
                result = 'EXISTING_RESEARCHED'
        decisions.append({'lead': index, 'result': result})
    return decisions


def preview_clay_import(csv_path, *, campaign='Sales Compensation Outreach', qualification='review'):
    payloads, report = normalize(read_csv(csv_path), campaign=campaign, qualification=qualification)
    if report['rejected']:
        return payloads, report, []
    require_signaldesk_project()
    companies = fetch_all_rows('gtm_companies', 'id,domain')
    contacts = fetch_all_rows('gtm_contacts', 'id,company_id,work_email,linkedin_url,contact_status')
    outreach = fetch_all_rows('gtm_outreach', 'id,contact_id,delivery_status')
    return payloads, report, classify_import(payloads, companies, contacts, outreach)


def suppression_rows(contacts, outreach, *, include_researched=False):
    sent_contact_ids = {r['contact_id'] for r in outreach if r.get('delivery_status') in ('sent','replied')}
    out = []
    for c in contacts:
        should_suppress = c.get('contact_status') in SUPPRESS_STATUSES or c.get('id') in sent_contact_ids
        if should_suppress or include_researched:
            reason = ('previously sent' if c.get('id') in sent_contact_ids
                      else c.get('contact_status') if should_suppress else 'already researched - reuse prior data')
            out.append({'Work Email': c.get('work_email') or '', 'LinkedIn Profile': c.get('linkedin_url') or '',
                        'Suppression Reason': reason})
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description='SignalDesk Clay CSV importer')
    cmds = parser.add_subparsers(dest='command', required=True)
    prep = cmds.add_parser('prepare', help='Convert actual Clay CSV columns to private JSONL')
    prep.add_argument('input_csv')
    prep.add_argument('--output', default='imports/leads.jsonl')
    prep.add_argument('--report', default='imports/prepare-report.json')
    prep.add_argument('--qualification', default='review', choices=['review','qualified','exclude'])
    prep.add_argument('--campaign', default='Sales Compensation Outreach')
    upload = cmds.add_parser('upload', help='Send prepared payloads to the Supabase RPC')
    upload.add_argument('input_jsonl')
    upload.add_argument('--apply', action='store_true', help='Explicitly apply prepared payloads through the RPC')
    clay = cmds.add_parser('import', help='Preview a Clay CSV against Supabase; write only with --apply')
    clay.add_argument('input_csv')
    clay.add_argument('--apply', action='store_true')
    clay.add_argument('--qualification', default='review', choices=['review','qualified','exclude'])
    clay.add_argument('--campaign', default='Sales Compensation Outreach')
    lookup = cmds.add_parser('lookup', help='Check an un-enriched Clay list against saved Supabase contacts (read-only)')
    lookup.add_argument('input_csv')
    lookup.add_argument('--output', default='imports/lookup-results.csv', help='Private CSV with all original rows and SignalDesk columns')
    suppress = cmds.add_parser('suppression', help='Export no-contact list from Supabase for Clay')
    suppress.add_argument('--output', default='imports/suppression.csv')
    suppress.add_argument('--include-researched', action='store_true', help='Also list researched contacts to avoid repeated paid enrichment')
    args = parser.parse_args(argv)
    if args.command == 'prepare':
        records, report = normalize(read_csv(args.input_csv), campaign=args.campaign, qualification=args.qualification)
        write_jsonl(args.output, records)
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'prepared_output': args.output, 'report': args.report, **report}, indent=2))
        if report['rejected']:
            return 2
    elif args.command == 'upload':
        if not args.apply:
            raise RuntimeError('Refusing live upload without --apply')
        require_signaldesk_project()
        print(json.dumps(upload_payloads(read_jsonl(args.input_jsonl), confirmed=True), indent=2))
    elif args.command == 'import':
        payloads, report, decisions = preview_clay_import(args.input_csv, campaign=args.campaign,
                                                         qualification=args.qualification)
        summary = {'mode': 'dry-run', 'input_rows': report['input_rows'], 'accepted': report['accepted'],
                   'rejected': report['rejected'], 'duplicates_in_csv': report['duplicates_in_csv'],
                   'decisions': decisions, 'counts': dict(Counter(d['result'] for d in decisions))}
        if report['rejected'] or any(d['result'] == 'CONFLICT_REVIEW' for d in decisions):
            print(json.dumps(summary, indent=2))
            return 2
        if args.apply:
            summary['mode'] = 'applied'
            summary['applied_counts'] = upload_payloads(payloads, confirmed=True)
        print(json.dumps(summary, indent=2))
    elif args.command == 'lookup':
        from contact_lookup import run_lookup
        return run_lookup(args.input_csv, args.output)
    elif args.command == 'suppression':
        contacts = fetch_all_rows('gtm_contacts', 'id,work_email,linkedin_url,contact_status')
        outreach = fetch_all_rows('gtm_outreach', 'id,contact_id,delivery_status')
        rows = suppression_rows(contacts, outreach, include_researched=args.include_researched)
        p = Path(args.output); p.parent.mkdir(parents=True, exist_ok=True)
        with p.open('w', encoding='utf-8', newline='') as f:
            w = csv.DictWriter(f, fieldnames=['Work Email','LinkedIn Profile','Suppression Reason']); w.writeheader(); w.writerows(rows)
        print(json.dumps({'suppressed_contacts':len(rows)}))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (RuntimeError, ValueError) as e:
        print(f'ERROR: {e}', file=sys.stderr)
        sys.exit(1)
