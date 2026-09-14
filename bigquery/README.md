# BigQuery demo dataset: row-level security keyed to Microsoft Entra identity

Two Microsoft Teams users ask the bot the **same question** and get
**different answers**, because BigQuery filters rows against their federated
Entra identity before the agent ever sees the data.

No per-user prompt. No agent-side filtering. No second tool. The same SQL,
byte for byte, returns a different result set depending on who is holding the
token.

## The narrative

1. Analyst opens Teams, asks the bot *"what is our total pipeline?"*
2. M365 admin opens Teams, asks the bot the **identical** question.
3. Two different numbers come back, and the analyst's is dramatically smaller.
4. The analyst then asks for one of the admin's deals **by name**. They get an
   empty result, not an error, not a refusal. The row simply does not exist
   as far as their session is concerned.

Step 4 is the one that lands with a security audience. The filtering happens
in BigQuery, below the agent. There is no instruction for a prompt injection
to override, because the model was never given the hidden rows in the first
place.

## Why this is not just "an agent with a WHERE clause"

The identity travels: Entra token → workforce identity pool → BigQuery
session → row access policy predicate. `SESSION_USER()` inside the policy
returns the caller's full federated principal URI, and the policy compares it
against the data.

Verified live through the BigQuery MCP server, the analyst's session returns:

```
principal://iam.googleapis.com/locations/global/workforcePools/teams-bot-demo/subject/<ANALYST_OBJECT_ID>
```

**Not an email address.** Any predicate written against
`analyst@<TENANT_DOMAIN>` matches zero rows. Store and compare the full
`principal://` URI. This is the single most common way to get this wrong.

## Files, in the order they run

| File | What it does |
|---|---|
| `01_dataset.sql` | Creates dataset `teams_bot_demo` in the **US** multi-region. |
| `02_tables.sql` | `sales_opportunities` (the business table) + `principal_access_map` (identity lookup). |
| `03_seed_data.sql` | 12 rows, deliberately lopsided. **Contains the admin OID placeholder.** |
| `04_row_access_policies.sql` | The RLS policies. Two approaches, one active. **Contains the placeholder too.** |
| `05_verify.sql` | Run interactively as each user. Not run by `apply.sh`. |
| `apply.sh` | Runs 01–04 in order. Idempotent. Refuses to run with the placeholder unfilled. |
| `NOTES.md` | What was actually executed live, and what was not. Read this before trusting anything. |

```bash
./apply.sh                       # once M365_ADMIN_OID is filled in
ALLOW_PLACEHOLDER=1 ./apply.sh   # analyst half only
```

Order is not negotiable: `CREATE OR REPLACE TABLE` in 02 silently drops every
row access policy on the table, so 04 must always follow it. Run the script,
not the individual files.

## ⚠️ You must fill in the admin's Entra object ID

`m365-admin@<TENANT_DOMAIN>`'s oid is **not known**. It appears as the
literal token `M365_ADMIN_OID` in `03_seed_data.sql` and
`04_row_access_policies.sql`.

Until it is replaced, **the admin user will see zero rows** and the demo has
no second half. `apply.sh` hard-fails on this rather than let you find out on
stage.

Find it:

```bash
az ad user show --id m365-admin@<TENANT_DOMAIN> --query id -o tsv
```

or, more trustworthy because it reports what BigQuery actually receives rather
than what Entra claims: have the admin ask the bot `SELECT SESSION_USER()` and
read the guid off the end of the returned URI.

Then:

```bash
sed -i 's/M365_ADMIN_OID/<the-real-guid>/g' 03_seed_data.sql 04_row_access_policies.sql
./apply.sh
```

## The data split

| Owner | Rows | Opportunity IDs |
|---|---|---|
| Analyst | 5 | `OPP-1xx` |
| M365 admin | 4 | `OPP-2xx` |
| Other reps (neither demo user) | 3 | `OPP-3xx` |

The `OPP-3xx` rows exist to make the admin's view provably "everything" rather
than "my own rows plus the analyst's" — without them the two policy designs
are indistinguishable in the output.

The two largest deals in the book sit in the admin's band, so the pipeline
totals differ by a large multiple rather than a few percent. A demo where the
numbers differ subtly is not a demo.

## Two policy designs

`04_row_access_policies.sql` contains both. **Approach A is active.**

**A — one generic policy (active).** Grants to the whole workforce pool, then
`FILTER USING` compares `SESSION_USER()` against `owner_principal`, plus a
subquery against `principal_access_map` for the admin override. Onboarding a
third user is an `INSERT`, not DDL. One predicate to debug instead of N.

**B — one policy per principal (commented out).** No subquery, so it stays
compatible with the BigQuery Storage Read API (Spark, Dataflow, BigFrames),
which does not support subquery predicates. Costs you: the admin's oid gets
hardcoded into DDL and every new user is another policy.

Both were confirmed to be accepted by BigQuery. The Teams bot uses the query
path, not the Storage Read API, so A's caveat does not bite this demo.

## Gotcha worth knowing before you demo

Once **any** row access policy exists on a table, a caller who matches **no**
grantee list sees **zero rows** — including the dataset owner. Confirmed live:
the account that created the table went from 12 rows to 0 the instant the
first policy landed.

`04` therefore adds a clearly-labelled break-glass policy so an operator can
still inspect the table. It is a demo convenience. Remove it, or replace it
with an audited group, before this pattern goes anywhere near real data.

An empty result is ambiguous between "RLS is working and you own nothing" and
"you are not a grantee at all". Always run the `SELECT SESSION_USER()` probe
first to tell them apart.

## Environment

- Project `<GCP_PROJECT_ID>` (<GCP_PROJECT_NUMBER>), org `organizations/<GCP_ORG_ID>`
- Workforce pool `locations/global/workforcePools/teams-bot-demo`
- Entra tenant `<ENTRA_TENANT_ID>` (<TENANT_DOMAIN>)
- Pool principalSet already holds `roles/bigquery.jobUser` and
  `roles/bigquery.dataViewer` on the project
- BigQuery MCP: `https://bigquery.googleapis.com/mcp`, streamable HTTP
  JSON-RPC, protocol `2025-06-18`, stateless. Tool `execute_sql_readonly`
  takes camelCase `projectId` / `query`. Header
  `X-Goog-User-Project: <GCP_PROJECT_ID>` is mandatory for workforce principals.

A user must hold table access **as well as** being on a grantee list. The
pool already has `dataViewer`, so this is satisfied — but if you rebuild the
pool, remember both halves are required or queries fail with access denied
rather than returning filtered rows.
