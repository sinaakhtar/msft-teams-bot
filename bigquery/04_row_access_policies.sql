-- 04_row_access_policies.sql
-- The row-level security that makes the demo work.
--
-- =========================================================================
-- WHAT WAS CONFIRMED LIVE (executed against <GCP_PROJECT_ID>, 2026-09-07)
-- =========================================================================
-- These are not inferences from documentation. Each was run as real DDL
-- against the real table and the real result recorded. See NOTES.md.
--
--   1. SESSION_USER() IS allowed inside a FILTER USING clause.
--      -> Policy created successfully. This is the important one: it means a
--         single generic policy keyed on SESSION_USER() is viable, and you do
--         NOT need one policy per user.
--
--   2. A single workforce identity IS namable in GRANT TO, in the
--      `principal://` form:
--         principal://iam.googleapis.com/locations/global/workforcePools/<WORKFORCE_POOL_ID>/subject/<oid>
--      -> Accepted.
--
--   3. The `principalSet://.../subject/<oid>` form is REJECTED. This form was
--      listed as a candidate in the original brief; it is wrong. BigQuery
--      returned, verbatim:
--         Invalid principalSet member
--         (principalSet://iam.googleapis.com/locations/global/workforcePools/<WORKFORCE_POOL_ID>/subject/<ANALYST_OBJECT_ID>)
--      Use `principal://` for one identity. `principalSet://` is only for
--      sets: /group/, /attribute./, and /*.
--
--   4. The whole-pool wildcard IS accepted in GRANT TO:
--         principalSet://iam.googleapis.com/locations/global/workforcePools/<WORKFORCE_POOL_ID>/*
--
--   5. A correlated subquery against a lookup table IS allowed inside
--      FILTER USING. -> Policy created successfully.
--
--   6. Once ANY policy exists on the table, a user who is not on any grantee
--      list sees ZERO rows, even the dataset OWNER. Verified: the account
--      that created the table went from 12 rows to 0 rows the moment the
--      first policy landed. This surprises people. See the break-glass
--      policy at the bottom.
--
-- WHAT IS NOT CONFIRMED
--   Whether the analyst actually sees exactly their own 5 rows when querying
--   as the federated Entra user. That requires a token for
--   analyst@<TENANT_DOMAIN>, which cannot be obtained from here. The
--   grammar is proven; the end-to-end enforcement is not. Run 05_verify.sql
--   as each user to close that gap.
--
-- Re-runnable: DROP ALL ROW ACCESS POLICIES first (verified working).

-- Clean slate so this file is safe to re-run.
DROP ALL ROW ACCESS POLICIES ON `<GCP_PROJECT_ID>.<BQ_DATASET>.sales_opportunities`;


-- =========================================================================
-- APPROACH A -- ONE GENERIC POLICY  (ACTIVE / RECOMMENDED)
-- =========================================================================
-- Grants to the entire workforce pool, then lets the FILTER decide what each
-- individual can see by comparing SESSION_USER() against the data.
--
-- Why this is the better design:
--   - Onboarding a third demo user is an INSERT into principal_access_map,
--     not a schema change. No DDL, no redeploy.
--   - There is exactly one predicate to reason about when something looks
--     wrong, instead of N policies whose union you have to work out.
--   - It does not need the admin's Entra oid baked into DDL.
--
-- The one real caveat, from the BigQuery docs: "Row access policies that
-- incorporate subqueries aren't compatible with the BigQuery Storage Read
-- API. The BigQuery Storage Read API only supports simple filter
-- predicates." The Teams bot goes through the query path (jobs.query via the
-- MCP server), not the Storage Read API, so this does not bite this demo.
-- It WOULD bite Spark / Dataflow / BigFrames readers against this table. If
-- you need those, switch to Approach B.

CREATE OR REPLACE ROW ACCESS POLICY rap_federated_identity
ON `<GCP_PROJECT_ID>.<BQ_DATASET>.sales_opportunities`
GRANT TO ("principalSet://iam.googleapis.com/locations/global/workforcePools/<WORKFORCE_POOL_ID>/*")
FILTER USING (
  -- Every federated user always sees the rows they own.
  owner_principal = SESSION_USER()
  -- ...plus, if the mapping table flags them as an admin, everything else.
  OR EXISTS (
    SELECT 1
    FROM `<GCP_PROJECT_ID>.<BQ_DATASET>.principal_access_map` AS m
    WHERE m.principal_uri = SESSION_USER()
      AND m.sees_all_rows
  )
);


-- =========================================================================
-- APPROACH B -- ONE POLICY PER PRINCIPAL  (INACTIVE -- commented out)
-- =========================================================================
-- Grammatically confirmed live: both statements below were accepted by
-- BigQuery in the `principal://` form. Left commented so that only one
-- approach is active at a time and the demo's behaviour is unambiguous.
--
-- Use this instead of Approach A if you need BigQuery Storage Read API
-- compatibility (Spark, Dataflow, BigFrames), since these predicates contain
-- no subquery.
--
-- Cost of this approach: the admin's Entra oid must be hardcoded into DDL,
-- so the <M365_ADMIN_OBJECT_ID> placeholder becomes a hard blocker rather than a
-- soft one, and every new user is another policy.
--
-- To switch: comment out Approach A above, uncomment the two statements
-- below, and fill in <M365_ADMIN_OBJECT_ID>.
--
-- CREATE OR REPLACE ROW ACCESS POLICY rap_analyst_own_rows
-- ON `<GCP_PROJECT_ID>.<BQ_DATASET>.sales_opportunities`
-- GRANT TO ("principal://iam.googleapis.com/locations/global/workforcePools/<WORKFORCE_POOL_ID>/subject/<ANALYST_OBJECT_ID>")
-- FILTER USING (owner_principal = SESSION_USER());
--
-- CREATE OR REPLACE ROW ACCESS POLICY rap_m365_admin_sees_all
-- ON `<GCP_PROJECT_ID>.<BQ_DATASET>.sales_opportunities`
-- GRANT TO ("principal://iam.googleapis.com/locations/global/workforcePools/<WORKFORCE_POOL_ID>/subject/<M365_ADMIN_OBJECT_ID>")
-- FILTER USING (TRUE);


-- =========================================================================
-- BREAK-GLASS -- keep the table inspectable by a first-party operator
-- =========================================================================
-- Finding 6 above: the moment a policy exists, everyone not on a grantee list
-- sees zero rows, including whoever owns the dataset. Without this, the next
-- person to open the table in the console sees an empty result and concludes
-- the seed failed.
--
-- Policies UNION, so this widens visibility for this one operator account and
-- changes nothing for the federated users.
--
-- This is a DEMO convenience. For anything holding real data, delete this or
-- replace it with a named break-glass group that is audited.

CREATE OR REPLACE ROW ACCESS POLICY rap_operator_breakglass
ON `<GCP_PROJECT_ID>.<BQ_DATASET>.sales_opportunities`
GRANT TO ("user:<GOOGLE_ADMIN_ACCOUNT>")
FILTER USING (TRUE);
