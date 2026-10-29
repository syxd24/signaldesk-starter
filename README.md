# SignalDesk — GTM Credit Saver

**Check your own prospect database *before* buying email enrichment again.**

Upload a Clay, Apollo or other lead-list CSV to the private local dashboard. SignalDesk checks saved contacts in **your own Supabase project**, reuses existing work emails where appropriate, and downloads a completed CSV with flags for missing emails, suppressions and identity conflicts.

**No SaaS subscription, no paid API calls, no automatic emails. Python standard library only.** The lookup itself is read-only. CSV export/import with your lead provider remains manual.

## Start in five steps

1. Install Python 3.10+ and download/copy this project.
2. In Supabase, create a **new empty project**. Open the SQL Editor and run `database/001_signaldesk.sql`. This creates four GTM tables and the import RPC with row-level security.
3. From the repository folder run `python src/dashboard.py --source demo`. Open **http://127.0.0.1:8765/** in your browser (type it directly).
4. Choose **Connect Supabase**. Enter *your own* project URL and private service-role key **in this local page only**, then choose **Test Connection**. The credential remains in the running Python process and disappears on restart.
5. Choose **Upload CSV**, select an UTF-8 file containing **LinkedIn Profile** or **Work Email** (5 MB / 10,000 rows maximum), click **Check Prospects**, then **Download Completed CSV**. Use the YES/NO flag to decide what still needs email enrichment.

**For a meaningful first demo:** after creating the schema, optionally run `database/seed_fictional_demo.sql` in that new demo-only project, then upload `sample-data/unenriched-prospects.csv`. Two fictional contacts already exist and one is new, so the lookup can demonstrate recovering previously saved emails. Otherwise a brand-new empty database will correctly flag everyone as new. Never seed a production database without reviewing the SQL.

## What goes into the completed CSV?

Every input row and input column is preserved, plus these new fields:

- `SignalDesk Row`: input row position
- `SignalDesk Match Key`: normalized identifier for joining
- `SignalDesk Status`: new, existing, contacted, do-not-contact or review
- `SignalDesk Saved Email` / `SignalDesk Final Email`: reused value when eligible
- `SignalDesk Saved Job Title` / `SignalDesk Saved Company`
- `SignalDesk Email Verification`: stored status, **not** a fresh verification
- `SignalDesk Enrich Email`: `YES` only when email enrichment is eligible and needed

Import the result as a **new** working table in Clay and **Save and don't run**. Add an email-enrichment condition using `SignalDesk Enrich Email = YES` to avoid buying already-known information. See [detailed CSV instructions](docs/PRE_ENRICHMENT_LOOKUP.md).

## Tests & optional terminal workflow

```powershell
python -m unittest discover -s tests -v
python src/dashboard.py --source demo
```

Alternatively, use `python src/signaldesk.py lookup your-list.csv --output imports/lookup.csv` with private environment credentials.

To populate your own long-term contact history, the optional CLI importer supports a read-only preview followed by explicitly approved `--apply`. **The dashboard does not write to Supabase**; a new empty database cannot recover emails until you import some saved contacts.

## Privacy and scope

- **Run locally only.** Do not deploy the private upload/connection interface to a public host. The static demo under `site/` is fictional and separate.
- The `service_role` credential grants privileged database access. Never paste it in chat, commit it, log it, or put it into public browser JavaScript. Only enter it into the local trusted dashboard bound to `127.0.0.1`.
- Only Supabase `https://PROJECT.supabase.co` URLs are accepted. Optionally denylist unrelated project IDs with `SIGNALDESK_BLOCKED_PROJECT_IDS` (comma-separated) in the **private** environment.
- Matching uses LinkedIn profile or work email, never name alone. Conflicts and suppressed contacts do not automatically receive reused emails.
- Exported files can contain personal data. Keep them private. `imports/` is Git-ignored.
- The credit-savings summary is an estimate; no savings are claimed unless independently measured.
- The project does **not** find missing emails, send messages, manage a hosted CRM or automatically synchronize with Clay.

## See how it looks

[View the fictional SignalDesk dashboard](https://signaldesk-gtm-demo.vercel.app/). The public site contains no private Supabase connection and does not process real CSVs.

## License

See `LICENSE` for reuse terms.
