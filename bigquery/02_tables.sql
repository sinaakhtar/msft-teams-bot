-- 02_tables.sql
-- The business table the demo asks questions about.
--
-- Design notes
-- ------------
-- `owner_principal` holds the FULL federated principal URI, not an email.
-- That is the load-bearing detail of this whole demo. It was verified live
-- that a workforce-federated user querying through the BigQuery MCP server
-- gets this back from SELECT SESSION_USER():
--
--   principal://iam.googleapis.com/locations/global/workforcePools/<WORKFORCE_POOL_ID>/subject/<ENTRA_OID>
--
-- Note the shape: scheme is `principal://` (singular, a single identity),
-- the pool is addressed WITHOUT a project number (workforce pools are
-- org-scoped, unlike workload identity pools), and the trailing segment is
-- the Entra object ID (oid), NOT the UPN / email. So any predicate that
-- compares against an email address will match zero rows. Store and compare
-- the full URI.
--
-- Re-runnable: CREATE OR REPLACE drops and recreates the table.
--
-- WARNING: CREATE OR REPLACE TABLE also drops every row access policy
-- attached to the table. That is convenient here (04 recreates them) but it
-- means this file must never be run on its own against a live demo without
-- re-running 04 afterwards, or the table is left wide open to anyone holding
-- dataViewer. apply.sh always runs 02 -> 03 -> 04 in order for this reason.

CREATE OR REPLACE TABLE `<GCP_PROJECT_ID>.<BQ_DATASET>.sales_opportunities`
(
  opportunity_id   STRING  NOT NULL OPTIONS (description = 'Stable synthetic opportunity key, OPP-###.'),
  account_name     STRING  NOT NULL OPTIONS (description = 'Customer account the opportunity sits under.'),
  region           STRING           OPTIONS (description = 'Sales region: EMEA / AMER / APAC.'),
  amount_usd       NUMERIC          OPTIONS (description = 'Deal value in USD. NUMERIC so SUM() is exact and the demo total is reproducible.'),
  stage            STRING           OPTIONS (description = 'Pipeline stage: Qualify / Propose / Negotiate / Closed Won.'),
  close_date       DATE             OPTIONS (description = 'Expected or actual close date.'),
  owner_principal  STRING  NOT NULL OPTIONS (description = 'FULL federated principal URI of the owning user, e.g. principal://iam.googleapis.com/locations/global/workforcePools/<WORKFORCE_POOL_ID>/subject/<entra-oid>. Compared directly against SESSION_USER() by the row access policies. NOT an email address.'),
  owner_label      STRING           OPTIONS (description = 'Human-readable owner name. Cosmetic only, for demo output. Never used in a security predicate.')
)
OPTIONS (
  description = 'Synthetic sales pipeline. Row-level security keyed to the querying users federated Entra identity: the analyst sees only rows they own, the M365 admin sees the whole book.'
);


-- ---------------------------------------------------------------------------
-- Optional: identity mapping table.
--
-- Only needed for the "one generic policy" approach in 04. It maps a
-- federated principal URI to what that principal is allowed to see. Keeping
-- the mapping in a table means onboarding a third demo user is an INSERT
-- rather than a new DDL policy.
--
-- Read 04_row_access_policies.sql before deciding whether you want this. The
-- subquery-based generic policy is the more elegant design but carries a real
-- caveat (BigQuery Storage Read API compatibility), documented there.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE TABLE `<GCP_PROJECT_ID>.<BQ_DATASET>.principal_access_map`
(
  principal_uri  STRING  NOT NULL OPTIONS (description = 'Full principal:// URI as returned by SESSION_USER() for this federated user.'),
  person_label   STRING           OPTIONS (description = 'Human-readable name, for demo readability only.'),
  sees_all_rows  BOOL    NOT NULL OPTIONS (description = 'TRUE = this principal is an admin and sees the entire table. FALSE = this principal sees only rows where owner_principal matches their own URI.')
)
OPTIONS (
  description = 'Lookup table mapping federated principal URIs to their row visibility. Consumed by the generic (subquery) row access policy in 04.'
);


-- ---------------------------------------------------------------------------
-- The zero-setup identity probe.
--
-- Run this FIRST in any demo, before touching the business table. It needs no
-- dataset, no table and no row access policy, so it isolates "is federation
-- working at all" from "is RLS working". Ask the Teams bot to run it as each
-- user.
--
--   SELECT SESSION_USER() AS querying_principal;
--
-- Expected for the analyst (verified live through the BigQuery MCP server):
--   principal://iam.googleapis.com/locations/global/workforcePools/<WORKFORCE_POOL_ID>/subject/<ANALYST_OBJECT_ID>
--
-- Expected for the M365 admin: the same URI shape with the admin's own Entra
-- oid in the subject/ segment. That oid is NOT yet known -- see README.
--
-- If this returns an email address instead of a principal:// URI, the request
-- did not arrive as a federated workforce principal and the rest of the demo
-- will not behave as intended. Fix that before debugging the policies.
-- ---------------------------------------------------------------------------
