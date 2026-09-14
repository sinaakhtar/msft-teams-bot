-- 05_verify.sql
-- The queries a human runs to PROVE the split. Run each one twice: once as
-- the analyst, once as the M365 admin, both through the Teams bot (or any
-- client that authenticates as the federated workforce principal).
--
-- =========================================================================
-- READ THIS BEFORE TRUSTING ANY OF IT
-- =========================================================================
-- Nothing below has been observed running as either federated Entra user.
-- It could not be: that needs a token for analyst@<TENANT_DOMAIN>, and
-- that user has no Google account of any kind. What HAS been verified live is
-- the DDL grammar, the policies existing on the table, and the contents of
-- the table read as a first-party operator. See NOTES.md for the exact
-- confirmed-vs-inferred split.
--
-- So: the expectations below are stated as SHAPES and RELATIONSHIPS, not as
-- numbers you should expect to match. If you see specific totals quoted
-- anywhere in this repo as "the analyst's answer", treat that as a bug.


-- -------------------------------------------------------------------------
-- STEP 1 -- Identity probe. Run this first, always.
-- -------------------------------------------------------------------------
-- Needs no table and no policy, so it separates "federation is broken" from
-- "RLS is misconfigured". Those two failures look identical downstream
-- (empty results) and this is the only cheap way to tell them apart.

SELECT SESSION_USER() AS querying_principal;

-- EXPECTED SHAPE, analyst:
--   principal://iam.googleapis.com/locations/global/workforcePools/<WORKFORCE_POOL_ID>/subject/<ANALYST_OBJECT_ID>
--   (this exact string was verified live through the BigQuery MCP server)
--
-- EXPECTED SHAPE, admin:
--   the same URI with a DIFFERENT guid in the subject/ segment.
--   Write that guid down. It is the <M365_ADMIN_OBJECT_ID> the rest of the setup
--   needs.
--
-- FAILURE MODE: if this returns an email address, the caller reached
-- BigQuery as a first-party Google identity, not as a federated workforce
-- principal. Stop and fix the auth path. No amount of policy tweaking will
-- help.


-- -------------------------------------------------------------------------
-- STEP 2 -- The money shot. Same question, two users, two answers.
-- -------------------------------------------------------------------------
-- This is the query to put on screen. Ask the bot, in both chats:
--   "What is our total pipeline?"

SELECT
  COUNT(*)          AS opportunities_visible,
  SUM(amount_usd)   AS pipeline_total_usd
FROM `<GCP_PROJECT_ID>.<BQ_DATASET>.sales_opportunities`;

-- EXPECTED RELATIONSHIP (not specific numbers):
--   * The analyst's row count is STRICTLY SMALLER than the admin's.
--   * The analyst's pipeline total is STRICTLY SMALLER than the admin's, and
--     dramatically so, not marginally. The seed puts the two largest deals in
--     the book outside the analyst's view on purpose, so the totals should
--     differ by roughly a factor of several, not by a few percent. If the two
--     numbers are close, the policies are probably not doing what you think.
--   * The admin's count should equal the full seeded row count, because the
--     admin is flagged sees_all_rows in the mapping table.
--   * NEITHER should be zero. Zero on both sides means no policy matched the
--     caller at all -- see step 5.
--
-- Both users run BYTE-FOR-BYTE the same SQL. That is the whole point. No
-- WHERE clause, no parameter, no per-user prompt engineering. The difference
-- comes entirely from who holds the token.


-- -------------------------------------------------------------------------
-- STEP 3 -- Show the seam. Which rows, not just how many.
-- -------------------------------------------------------------------------

SELECT
  opportunity_id,
  account_name,
  region,
  amount_usd,
  stage,
  owner_label
FROM `<GCP_PROJECT_ID>.<BQ_DATASET>.sales_opportunities`
ORDER BY amount_usd DESC;

-- EXPECTED SHAPE:
--   * Analyst: only rows whose owner_label is the analyst's. Every
--     opportunity_id should be in the OPP-1xx band.
--   * Admin: rows across all three owner bands -- OPP-1xx (analyst),
--     OPP-2xx (admin) and OPP-3xx (other reps). The OPP-3xx rows are the
--     useful tell: they belong to neither demo user, so if the admin can see
--     them the policy is genuinely "see everything" rather than "see my own
--     plus the analyst's".
--   * The single largest-value row must be absent from the analyst's result
--     and present in the admin's.


-- -------------------------------------------------------------------------
-- STEP 4 -- Prove the filter is server-side, not prompt-side.
-- -------------------------------------------------------------------------
-- A sceptical audience will assume the agent is filtering, or that the model
-- was told to withhold rows. Have the analyst ask for the hidden data
-- directly and by name.

SELECT *
FROM `<GCP_PROJECT_ID>.<BQ_DATASET>.sales_opportunities`
WHERE opportunity_id = 'OPP-201';   -- a deal the analyst does not own

-- EXPECTED: zero rows for the analyst, one row for the admin.
--
-- Note the failure mode this demonstrates: the analyst does not get
-- "permission denied", they get an EMPTY RESULT. Row-level security is
-- silent. The row is not redacted or flagged, it simply is not in the
-- result set, and the agent has no way to know it existed. That is the
-- correct behaviour and it is worth saying out loud during the demo,
-- because it is also what makes RLS safe to put behind an LLM: there is
-- nothing for a prompt injection to talk the agent out of.


-- -------------------------------------------------------------------------
-- STEP 5 -- Debugging when both users see nothing
-- -------------------------------------------------------------------------
-- Run as an operator who is on the break-glass policy.

-- 5a. What policies actually exist?
--   bq ls --row_access_policies <GCP_PROJECT_ID>:<BQ_DATASET>.sales_opportunities

-- 5b. Does the stored owner_principal match the URI shape SESSION_USER()
--     returns? The usual bug is a stored email, a stored UPN, or a stale
--     <M365_ADMIN_OBJECT_ID> placeholder that was never substituted.
SELECT DISTINCT owner_principal, owner_label
FROM `<GCP_PROJECT_ID>.<BQ_DATASET>.sales_opportunities`
ORDER BY owner_label;

-- 5c. Is the mapping table still carrying the unsubstituted placeholder?
--     If this returns any row, the admin will see ZERO rows, because the
--     literal token '<M365_ADMIN_OBJECT_ID>' can never equal a real SESSION_USER().
SELECT *
FROM `<GCP_PROJECT_ID>.<BQ_DATASET>.principal_access_map`
WHERE principal_uri LIKE '%<M365_ADMIN_OBJECT_ID>%';

-- 5d. Reminder of a non-obvious behaviour, confirmed live: once ANY row
--     access policy exists on a table, a caller who matches NO grantee list
--     sees zero rows -- including the dataset owner. An empty result is
--     therefore ambiguous between "policy working, you own nothing" and
--     "you are not a grantee at all". Step 1 disambiguates.
