# New Clay campaign: reuse saved emails before enrichment

## The five-to-ten-column question

You do **not** need to modify your original five-column Clay table.

1. In Clay, build your new list with minimal columns. Include **LinkedIn Profile** whenever possible (or **Work Email** if you already have it). Names alone cannot safely identify a person.
2. **Before running email enrichment**, export your original list as CSV.
3. Run the read-only lookup in the trusted SignalDesk PowerShell terminal:

   ```powershell
   python src/signaldesk.py lookup "$HOME\Downloads\new-prospects.csv" --output imports/new-campaign-lookup.csv
   ```

   As with the other private Supabase commands, set your SignalDesk SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in this terminal first.
4. Python preserves **every input row and every input column**; then adds these columns:
   - `SignalDesk Row`: original row position for audit
   - `SignalDesk Match Key`: normalized LinkedIn URL or email (for optional lookup back into the original table)
   - `SignalDesk Status`: existing/new/review/contacted/do-not-contact
   - `SignalDesk Saved Email`: email stored in Supabase, if reusable
   - `SignalDesk Final Email`: email from the new list or saved contact; blank when blocked
   - `SignalDesk Saved Job Title`, `SignalDesk Saved Company`
   - `SignalDesk Email Verification`: last stored verification state, not a claim that emails are freshly verified
   - `SignalDesk Enrich Email`: YES or NO
5. In Clay choose **Create new table → Import from CSV** and upload `imports/new-campaign-lookup.csv`. Map its columns and select **Save and don't run**. Keep the original Clay table unchanged.
6. Add the email enrichment column to this **new** working table. Enable it only if `SignalDesk Enrich Email` is `YES`. With the condition in place, enrich only eligible rows with missing emails.
7. When your Clay research is done, export the finished list and run the existing `import` preview and then `--apply` if you approve it. Set a distinct `--campaign` name for the new campaign.

Example: 100 original rows → 100 output rows. If 30 have reusable email addresses, 60 are new and missing emails, 5 already exist without emails and 5 need suppression or identity review, the lookup outputs all 100 rows, with 65 marked YES for email enrichment. These are illustrative counts.

## Statuses

| Status | Meaning | Enrich email? |
|---|---|---|
| EXISTING_EMAIL | Match found and usable email available | NO |
| EXISTING_MISSING_EMAIL | Match found but no saved email | YES |
| NEW_EMAIL_PRESENT | New contact, input CSV already has an email | NO |
| NEW_EMAIL_NEEDED | New contact with LinkedIn but no email | YES |
| ALREADY_CONTACTED | Global safety guard from prior campaigns | NO |
| DO_NOT_CONTACT | Global suppression | NO |
| REVIEW_* | Invalid, repeated, inconsistent or incomplete identity | NO until resolved |

This lookup **reads Supabase** and writes only a local private CSV. It never changes any database rows, sends outreach, or calls a paid enrichment provider. Source emails are not independently verified; confirm them before outreach.

## Important limitations

- LinkedIn URL or work email is required to match. Full name + company is **not** sufficient.
- The existing SignalDesk database does not contain a company's entire Clay enrichment output: only fields currently persisted in Supabase can be reused.
- The CSV must remain private. `imports/` is excluded from Git. Never commit a real prospect export.
- The returned table is a **new Clay table**. Importing as **Add to current table** would append duplicate prospect rows instead of filling original rows.
- The new campaign can create draft sequences, but globally contacted and do-not-contact contacts remain suppressed by current policy. Campaign-level cooldown/eligibility is a separate future feature.
- Clay Free plan does not provide this project with an authenticated two-way sync, so CSV export/import is manual.

### Optional: keep using the original Clay table

Instead of creating a replacement table, import the lookup CSV as a separate reference table and add a **Lookup single row** column to your original Clay table, matching by normalized LinkedIn URL. Extract the returned saved fields into new columns. Only run enrichments where saved values are blank. This requires additional manual Clay setup; the new full CSV working table is the simplest initial approach.
