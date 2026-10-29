import csv
import importlib.util
import io
import json
import os
import sys
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('signaldesk', Path(__file__).resolve().parents[1] / 'src' / 'signaldesk.py')
s = importlib.util.module_from_spec(spec); spec.loader.exec_module(s)

BASE = {'Company':'ExampleAI','Full Name':'Demo Person','Work Email':' Demo@ExampleAI.Com ',
        'LinkedIn Profile':'https://www.linkedin.com/in/demo-person/?trk=foo',
        'Job Title':'Chief Revenue Officer','Template Email 1':'Saw ExampleAI is hiring a Sales Compensation Manager.\n\nQuestion?'}

class PipelineTests(unittest.TestCase):
    def test_email_and_linkedin_normalized(self):
        rows,report=s.normalize([BASE]); self.assertEqual(report['accepted'],1)
        self.assertEqual(rows[0]['work_email'],'demo@exampleai.com')
        self.assertEqual(rows[0]['linkedin_url'],'https://www.linkedin.com/in/demo-person')
    def test_domain_derived_and_flagged(self):
        _,report=s.normalize([BASE]);self.assertEqual(report['domain_inferred_from_email'],1)
    def test_company_signal_from_second_person(self):
        r1=dict(BASE);r2=dict(BASE,**{'Full Name':'Other Person','Work Email':'other@exampleai.com','Use AI Hiring Signal':'Hiring sales comp lead'})
        rows,_=s.normalize([r1,r2]);self.assertEqual(rows[0]['hiring_signal'],'Hiring sales comp lead')
    def test_duplicates_by_email(self):
        r=dict(BASE,**{'Full Name':'Another'})
        rows,rep=s.normalize([BASE,r]);self.assertEqual(len(rows),1);self.assertEqual(len(rep['duplicates_in_csv']),1)
    def test_duplicates_by_linkedin(self):
        r=dict(BASE,**{'Work Email':'another@exampleai.com'})
        rows,rep=s.normalize([BASE,r]);self.assertEqual(len(rows),1)
    def test_conflict_same_email_different_linkedin_rejected(self):
        other = dict(BASE, **{'Full Name': 'Different Person',
                              'LinkedIn Profile': 'https://linkedin.com/in/not-demo-person'})
        rows, report = s.normalize([BASE, other])
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(report['rejected']), 1)
        self.assertIn('different LinkedIn', report['rejected'][0]['reasons'][0])

    def test_conflict_same_linkedin_different_emails_rejected(self):
        other = dict(BASE, **{'Full Name': 'Different Person', 'Work Email': 'new@exampleai.com'})
        rows, report = s.normalize([BASE, other])
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(report['rejected']), 1)
        self.assertIn('different work emails', report['rejected'][0]['reasons'][0])

    def test_conflicting_identifiers_that_would_merge_two_people_rejected(self):
        second = dict(BASE, **{'Full Name':'Another', 'Work Email':'another@exampleai.com',
                                'LinkedIn Profile':'https://linkedin.com/in/other'})
        third = dict(BASE, **{'Work Email':'another@exampleai.com'})
        rows, report = s.normalize([BASE, second, third])
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(report['rejected']), 1)

    def test_repeat_enriches_first_record_with_missing_identifier(self):
        first = dict(BASE, **{'Work Email':'', 'Company Domain':'exampleai.com'})
        second = dict(BASE, **{'Work Email':'demo@exampleai.com'})
        rows, report = s.normalize([first, second])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['work_email'], 'demo@exampleai.com')
        self.assertEqual(len(report['duplicates_in_csv']), 1)

    def test_conflict_same_identity_two_company_domains_rejected(self):
        first = dict(BASE, **{'Company Domain':'exampleai.com'})
        second = dict(BASE, **{'Company':'OtherCorp','Company Domain':'othercorp.com'})
        rows, report = s.normalize([first, second])
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(report['rejected']), 1)

    def test_mock_upload_repeat_returns_existing_without_second_insert(self):
        from unittest.mock import patch
        outcomes = [{'result':'NEW'}, {'result':'EXISTING_RESEARCHED'}]
        with patch.object(s, 'api_request', side_effect=outcomes) as call:
            first = s.upload_payloads([{'full_name':'Demo Person'}], confirmed=True)
            second = s.upload_payloads([{'full_name':'Demo Person'}], confirmed=True)
        self.assertEqual(first, {'NEW':1})
        self.assertEqual(second, {'EXISTING_RESEARCHED':1})
        self.assertEqual(call.call_count, 2)
        self.assertEqual(call.call_args.args[1], 'rpc/gtm_ingest_lead')

    def test_prepared_report_contains_no_personal_details(self):
        rows, report = s.normalize([BASE, dict(BASE, **{'Work Email':'bad'})])
        as_text = json.dumps(report)
        self.assertNotIn('demo@exampleai.com', as_text)
        self.assertNotIn('Demo Person', as_text)
        self.assertEqual(len(report['rejected']), 1)

    def test_prepare_produces_report_file_without_network(self):
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'leads.csv'
            with source.open('w', encoding='utf-8', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=list(BASE))
                writer.writeheader()
                writer.writerow(BASE)
            output = Path(folder) / 'leads.jsonl'
            report = Path(folder) / 'report.json'
            with patch.object(s, 'api_request', side_effect=AssertionError('Should never use network')):
                status = s.main(['prepare', str(source), '--output', str(output), '--report', str(report)])
            self.assertEqual(status, 0)
            self.assertEqual(len(s.read_jsonl(output)), 1)
            self.assertEqual(json.loads(report.read_text())['accepted'], 1)

    def test_missing_identifiers_rejected(self):
        r=dict(BASE,**{'Work Email':'','LinkedIn Profile':''});rows,rep=s.normalize([r]);self.assertEqual(rep['accepted'],0)
    def test_invalid_email_rejected(self):
        rows,rep=s.normalize([dict(BASE,**{'Work Email':'foo@x'})]);self.assertEqual(len(rows),0)
    def test_invalid_linkedin_rejected(self):
        rows,rep=s.normalize([dict(BASE,**{'LinkedIn Profile':'https://evil.test/in/person'})]);self.assertEqual(len(rows),0)
    def test_never_claims_verified(self):
        rows,_=s.normalize([BASE]);self.assertEqual(rows[0]['email_verification'],'unknown')
    def test_unapproved_emails_are_not_sent(self):
        rows,_=s.normalize([BASE]);self.assertNotIn('delivery_status',rows[0]); self.assertIn('email_1_body',rows[0])
    def test_live_upload_requires_approval(self):
        with self.assertRaises(RuntimeError):s.upload_payloads([{'a':1}])
    def test_request_error_does_not_echo_secret(self):
        env = {'SUPABASE_URL': 'https://demo-project.supabase.co', 'SUPABASE_SERVICE_ROLE_KEY': 'secret-test-value'}
        with patch.dict(os.environ, env), patch.object(s, 'urlopen', side_effect=ValueError('secret-test-value')):
            with self.assertRaises(RuntimeError) as error:
                s.api_request('GET', 'gtm_contacts?select=id')
        self.assertNotIn('secret-test-value', str(error.exception))
    def test_suppression_includes_sent_and_do_not_contact(self):
        people=[{'id':'1','contact_status':'researched','work_email':'a@example.invalid'}, {'id':'2','contact_status':'do_not_contact','work_email':'b@example.invalid'}]
        mail=[{'contact_id':'1','delivery_status':'sent'}]
        rows=s.suppression_rows(people,mail);self.assertEqual(len(rows),2)
    def test_suppression_fetch_all_pages_without_truncation(self):
        from unittest.mock import patch
        rows = [{'id': str(i)} for i in range(450)]
        def fake_request(method, path, payload=None):
            self.assertEqual(method, 'GET')
            start = int(path.rsplit('offset=', 1)[1])
            return rows[start:start+200]
        with patch.object(s, 'api_request', side_effect=fake_request) as mocked:
            result = s.fetch_all_rows('gtm_contacts','id,work_email',page_size=200)
        self.assertEqual(result, rows)
        self.assertEqual(mocked.call_count, 3)

    def test_suppression_fetch_exact_multiple_requests_empty_final_page(self):
        from unittest.mock import patch
        rows = [{'id': str(i)} for i in range(400)]
        def fake_request(method, path, payload=None):
            start = int(path.rsplit('offset=', 1)[1])
            return rows[start:start+200]
        with patch.object(s, 'api_request', side_effect=fake_request) as mocked:
            result = s.fetch_all_rows('gtm_contacts','id',page_size=200)
        self.assertEqual(len(result), 400)
        self.assertEqual(mocked.call_count, 3)

    def test_suppression_rejects_unexpected_api_shape(self):
        from unittest.mock import patch
        with patch.object(s, 'api_request', return_value={'error':'invalid'}):
            with self.assertRaises(RuntimeError):
                s.fetch_all_rows('gtm_contacts','id')

    def test_already_seen_export_includes_researched_without_marking_contacted(self):
        people = [{'id':'1','contact_status':'researched','work_email':'one@example.invalid'}]
        rows = s.suppression_rows(people, [], include_researched=True)
        self.assertEqual(len(rows), 1)
        self.assertIn('already researched', rows[0]['Suppression Reason'])
        self.assertNotIn('contacted', rows[0]['Suppression Reason'])

    def test_suppression_rows_do_not_suppress_unsent_drafts(self):
        people = [{'id':'1','contact_status':'researched','work_email':'one@example.invalid'}]
        mail = [{'contact_id':'1','delivery_status':'draft'}]
        self.assertEqual(s.suppression_rows(people, mail), [])

    def test_clean_row_review_by_default(self):
        rows,_=s.normalize([BASE]);self.assertEqual(rows[0]['qualification_status'],'review')
    def test_personal_email_domain_not_used_as_company_domain(self):
        row=dict(BASE, **{'Work Email':'person@gmail.com'})
        rows, rep=s.normalize([row]);self.assertEqual(len(rows),0)
    def test_empty_company_rejected(self):
        rows,rep=s.normalize([dict(BASE,Company='')]);self.assertEqual(len(rows),0)
    def test_preserves_email_template(self):
        rows,_=s.normalize([BASE]);self.assertEqual(rows[0]['email_1_body'],BASE['Template Email 1'])

    def test_clay_preview_classifies_all_remote_statuses(self):
        leads = [dict(BASE, **{'Work Email': f'person{i}@exampleai.com',
                               'LinkedIn Profile': f'https://linkedin.com/in/person{i}'}) for i in range(4)]
        payloads, report = s.normalize(leads)
        self.assertEqual(report['accepted'], 4)
        companies = [{'id': 'company', 'domain': 'exampleai.com'}]
        contacts = [{'id': str(i), 'company_id': 'company', 'work_email': f'person{i}@exampleai.com',
                     'linkedin_url': f'https://linkedin.com/in/person{i}', 'contact_status': status}
                    for i, status in enumerate(('researched', 'researched', 'contacted', 'do_not_contact'))]
        contacts = contacts[1:]
        outreach = [{'contact_id': '1', 'delivery_status': 'draft'},
                    {'contact_id': '2', 'delivery_status': 'sent'}]
        decisions = s.classify_import(payloads, companies, contacts, outreach)
        self.assertEqual([d['result'] for d in decisions],
                         ['NEW', 'EXISTING_RESEARCHED', 'ALREADY_CONTACTED', 'DO_NOT_CONTACT'])

    def test_clay_preview_flags_remote_identity_conflict(self):
        payloads, _ = s.normalize([BASE])
        companies = [{'id': 'company', 'domain': 'exampleai.com'}]
        contacts = [{'id': 'one', 'company_id': 'company', 'work_email': 'demo@exampleai.com',
                     'linkedin_url': 'https://linkedin.com/in/someone-else', 'contact_status': 'researched'}]
        self.assertEqual(s.classify_import(payloads, companies, contacts, [])[0]['result'], 'CONFLICT_REVIEW')

    def test_clay_import_dry_run_does_not_write_or_print_private_data(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'leads.csv'
            with source.open('w', encoding='utf-8', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=list(BASE)); writer.writeheader(); writer.writerow(BASE)
            output = io.StringIO()
            env = {'SUPABASE_URL': 'https://demo-project.supabase.co', 'SUPABASE_SERVICE_ROLE_KEY': 'test-secret'}
            with patch.dict(os.environ, env), patch.object(s, 'fetch_all_rows', side_effect=[[], [], []]) as reads, \
                 patch.object(s, 'upload_payloads', side_effect=AssertionError('No writes')), redirect_stdout(output):
                status = s.main(['import', str(source)])
            self.assertEqual(status, 0)
            self.assertEqual(reads.call_count, 3)
            self.assertEqual(json.loads(output.getvalue())['decisions'], [{'lead': 1, 'result': 'NEW'}])
            self.assertNotIn('Demo Person', output.getvalue())
            self.assertNotIn('test-secret', output.getvalue())

    def test_clay_import_apply_uses_existing_rpc_path(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'leads.csv'
            with source.open('w', encoding='utf-8', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=list(BASE)); writer.writeheader(); writer.writerow(BASE)
            output = io.StringIO()
            env = {'SUPABASE_URL': 'https://demo-project.supabase.co', 'SUPABASE_SERVICE_ROLE_KEY': 'test-secret'}
            with patch.dict(os.environ, env), patch.object(s, 'fetch_all_rows', side_effect=[[], [], []]), \
                 patch.object(s, 'api_request', return_value={'result': 'NEW'}) as request, redirect_stdout(output):
                status = s.main(['import', str(source), '--apply'])
            self.assertEqual(status, 0)
            self.assertEqual(request.call_count, 1)
            self.assertEqual(request.call_args.args[0:2], ('POST', 'rpc/gtm_ingest_lead'))
            self.assertEqual(json.loads(output.getvalue())['applied_counts'], {'NEW': 1})

    def test_clay_import_rejects_bad_rows_before_network_or_apply(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'leads.csv'
            with source.open('w', encoding='utf-8', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=list(BASE)); writer.writeheader()
                writer.writerow(dict(BASE, **{'Work Email': '', 'LinkedIn Profile': ''}))
            with patch.object(s, 'api_request', side_effect=AssertionError('No network')), redirect_stdout(io.StringIO()):
                self.assertEqual(s.main(['import', str(source), '--apply']), 2)

    def test_clay_import_conflict_prevents_apply(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'leads.csv'
            with source.open('w', encoding='utf-8', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=list(BASE)); writer.writeheader(); writer.writerow(BASE)
            company = [{'id': 'company', 'domain': 'exampleai.com'}]
            contacts = [{'id': 'one', 'company_id': 'company', 'work_email': 'demo@exampleai.com',
                         'linkedin_url': 'https://linkedin.com/in/other', 'contact_status': 'researched'}]
            env = {'SUPABASE_URL': 'https://demo-project.supabase.co', 'SUPABASE_SERVICE_ROLE_KEY': 'test-secret'}
            output = io.StringIO()
            with patch.dict(os.environ, env), patch.object(s, 'fetch_all_rows', side_effect=[company, contacts, []]), \
                 patch.object(s, 'upload_payloads', side_effect=AssertionError('No writes')), redirect_stdout(output):
                self.assertEqual(s.main(['import', str(source), '--apply']), 2)
            self.assertEqual(json.loads(output.getvalue())['decisions'][0]['result'], 'CONFLICT_REVIEW')

if __name__ == '__main__': unittest.main()
