-- OPTIONAL: fictional demonstration records for a NEW, empty SignalDesk Supabase project.
-- Run AFTER 001_signaldesk.sql in the Supabase SQL Editor.
-- These identities/domains are invented and must not be used for real outreach.
insert into public.gtm_companies (name, domain, qualification_status, hiring_signal)
values ('DemoSoft', 'demosoft.example', 'qualified', 'Fictional sales compensation hiring')
on conflict (domain) do nothing;

insert into public.gtm_contacts
    (company_id, full_name, job_title, linkedin_url, work_email, email_verification, contact_status)
select c.id, sample.full_name, sample.job_title, sample.linkedin_url, sample.work_email,
       'unknown', 'researched'
from public.gtm_companies c
cross join (values
    ('Jordan Example', 'Chief Revenue Officer', 'https://www.linkedin.com/in/demo-jordan', 'jordan@demosoft.example'),
    ('Casey Example', 'Vice President of Sales', 'https://www.linkedin.com/in/demo-casey', 'casey@demosoft.example')
) as sample(full_name,job_title,linkedin_url,work_email)
where c.domain='demosoft.example'
  and not exists (
    select 1 from public.gtm_contacts existing
    where lower(existing.work_email)=lower(sample.work_email)
       or lower(existing.linkedin_url)=lower(sample.linkedin_url)
  );
