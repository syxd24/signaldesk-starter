# Free-plan suppression workflow (manual Clay CSV round-trip)

1. Import a Clay People table CSV through the local `prepare` command; use the private upload command **only after reviewing the dry-run and setting credentials in a private environment**. The database uses work email and LinkedIn profile, not names alone, to identify contacts.
2. Export the previously contacted / opted-out list: `python src/signaldesk.py suppression --output imports/do-not-contact.csv`. This reads paginated contact and outreach data from Supabase. Only upload with server-side credentials in a trusted terminal.
3. For **avoiding unnecessary paid research**, use `python src/signaldesk.py suppression --include-researched --output imports/already-seen.csv`. This includes researched-but-not-contacted people too; their reason says `already researched - reuse prior data`, not `contacted`.
4. In Clay, create a separate table by importing the appropriate CSV. In your new People table, run `Look up single row in other table` using matching **Work Email**; if missing, use **LinkedIn Profile** (with consistent normalization). Use the returned `Suppression Reason` to decide whether to skip email enrichment or outreach. A missing match is not a guarantee a lead is new.
5. Clay Free does not automatically sync with Supabase. Refresh the exported lookup table before your next campaign. Do not send emails from this project; this is only a portfolio demonstration.

**Safety:** `do-not-contact.csv` and `already-seen.csv` contain personal contact information and are generated only under ignored `imports/`. Do not upload them to public GitHub. Pagination is covered by mocked local tests, not yet a credentialed end-to-end local smoke test.
