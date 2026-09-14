-- 03_seed_data.sql
-- Seeds the demo pipeline. 12 rows, deliberately lopsided.
--
-- The split is engineered so that "what is our total pipeline?" is a visibly
-- different answer per user rather than a subtly different one. The analyst
-- owns a handful of mid-size deals; the two largest deals in the book belong
-- to the admin and the analyst can never see them. A demo where the two
-- totals differ by 3% is not a demo. This one differs by roughly a factor of
-- five.
--
-- !!! PLACEHOLDER THAT MUST BE FILLED IN BEFORE THIS DEMO WORKS END-TO-END !!!
--
--   The M365 admin's Entra object ID is NOT known yet. Everywhere below it
--   appears as the literal token  <M365_ADMIN_OBJECT_ID>  inside the principal URI.
--   Nothing involving the admin user will work until it is replaced.
--
--   To find it, run as a tenant admin:
--       az ad user show --id m365-admin@<TENANT_DOMAIN> --query id -o tsv
--   or via Graph:
--       GET https://graph.microsoft.com/v1.0/users/m365-admin@<TENANT_DOMAIN>?$select=id
--   or simply have the admin user ask the Teams bot:
--       SELECT SESSION_USER()
--   and read the subject/ segment off the returned URI. That last option is
--   the most trustworthy, because it reports what BigQuery actually receives
--   rather than what Entra claims to hold.
--
--   Then substitute it in this file and in 04_row_access_policies.sql:
--       sed -i 's/<M365_ADMIN_OBJECT_ID>/<the-real-guid>/g' 03_seed_data.sql 04_row_access_policies.sql
--
--   The analyst's oid below is real and verified. The admin's is not.
--
-- Re-runnable: TRUNCATE before INSERT, so re-running does not duplicate rows.

TRUNCATE TABLE `<GCP_PROJECT_ID>.<BQ_DATASET>.sales_opportunities`;

INSERT INTO `<GCP_PROJECT_ID>.<BQ_DATASET>.sales_opportunities`
  (opportunity_id, account_name, region, amount_usd, stage, close_date, owner_principal, owner_label)
VALUES
  -- ---------------------------------------------------------------------
  -- Owned by the ANALYST  (analyst@<TENANT_DOMAIN>)
  -- Entra oid <ANALYST_OBJECT_ID> -- VERIFIED LIVE.
  -- 5 rows. This is the analyst's entire visible world.
  -- ---------------------------------------------------------------------
  ('OPP-101', 'Northwind Traders',   'EMEA',  48000.00, 'Propose',    DATE '2026-10-15',
   'principal://iam.googleapis.com/locations/global/workforcePools/<WORKFORCE_POOL_ID>/subject/<ANALYST_OBJECT_ID>', 'Analyst'),
  ('OPP-102', 'Fabrikam Nordic',     'EMEA',  72500.00, 'Negotiate',  DATE '2026-09-30',
   'principal://iam.googleapis.com/locations/global/workforcePools/<WORKFORCE_POOL_ID>/subject/<ANALYST_OBJECT_ID>', 'Analyst'),
  ('OPP-103', 'Tailspin Logistics',  'EMEA',  31250.00, 'Qualify',    DATE '2026-12-01',
   'principal://iam.googleapis.com/locations/global/workforcePools/<WORKFORCE_POOL_ID>/subject/<ANALYST_OBJECT_ID>', 'Analyst'),
  ('OPP-104', 'Proseware Retail',    'AMER',  95000.00, 'Propose',    DATE '2026-11-20',
   'principal://iam.googleapis.com/locations/global/workforcePools/<WORKFORCE_POOL_ID>/subject/<ANALYST_OBJECT_ID>', 'Analyst'),
  ('OPP-105', 'Contoso Field Svcs',  'EMEA',  18750.00, 'Closed Won', DATE '2026-08-28',
   'principal://iam.googleapis.com/locations/global/workforcePools/<WORKFORCE_POOL_ID>/subject/<ANALYST_OBJECT_ID>', 'Analyst'),

  -- ---------------------------------------------------------------------
  -- Owned by the M365 ADMIN  (m365-admin@<TENANT_DOMAIN>)
  -- oid UNKNOWN -- the <M365_ADMIN_OBJECT_ID> token below is a placeholder.
  -- 4 rows, including the two whale deals that drive the totals apart.
  -- ---------------------------------------------------------------------
  ('OPP-201', 'Globex Manufacturing','AMER', 640000.00, 'Negotiate',  DATE '2026-10-31',
   'principal://iam.googleapis.com/locations/global/workforcePools/<WORKFORCE_POOL_ID>/subject/<M365_ADMIN_OBJECT_ID>', 'M365 Admin'),
  ('OPP-202', 'Initech Financial',   'AMER', 415000.00, 'Propose',    DATE '2026-12-15',
   'principal://iam.googleapis.com/locations/global/workforcePools/<WORKFORCE_POOL_ID>/subject/<M365_ADMIN_OBJECT_ID>', 'M365 Admin'),
  ('OPP-203', 'Umbrella Health',     'EMEA',  88000.00, 'Qualify',    DATE '2027-01-20',
   'principal://iam.googleapis.com/locations/global/workforcePools/<WORKFORCE_POOL_ID>/subject/<M365_ADMIN_OBJECT_ID>', 'M365 Admin'),
  ('OPP-204', 'Vandelay Imports',    'APAC',  54000.00, 'Propose',    DATE '2026-11-05',
   'principal://iam.googleapis.com/locations/global/workforcePools/<WORKFORCE_POOL_ID>/subject/<M365_ADMIN_OBJECT_ID>', 'M365 Admin'),

  -- ---------------------------------------------------------------------
  -- Owned by OTHER reps -- neither demo user owns these.
  -- 3 rows. Their job is to prove the admin's view is genuinely "everything"
  -- and not merely "my own rows too". Without these, an admin policy of
  -- `owner_principal = SESSION_USER() OR <analyst rows>` would be
  -- indistinguishable from a real see-all policy.
  -- These oids are fictional and intentionally so.
  -- ---------------------------------------------------------------------
  ('OPP-301', 'Soylent Foods',       'APAC', 127000.00, 'Negotiate',  DATE '2026-10-09',
   'principal://iam.googleapis.com/locations/global/workforcePools/<WORKFORCE_POOL_ID>/subject/00000000-0000-4000-8000-00000000c301', 'Rep C'),
  ('OPP-302', 'Wayne Industrial',    'AMER', 233000.00, 'Propose',    DATE '2026-11-27',
   'principal://iam.googleapis.com/locations/global/workforcePools/<WORKFORCE_POOL_ID>/subject/00000000-0000-4000-8000-00000000c302', 'Rep D'),
  ('OPP-303', 'Stark Materials',     'EMEA',  61500.00, 'Qualify',    DATE '2027-02-10',
   'principal://iam.googleapis.com/locations/global/workforcePools/<WORKFORCE_POOL_ID>/subject/00000000-0000-4000-8000-00000000c303', 'Rep E');


-- ---------------------------------------------------------------------------
-- Identity mapping table.
-- Only consumed by the generic (subquery) row access policy in 04. Harmless
-- to populate even if you go with the per-principal policies instead.
-- ---------------------------------------------------------------------------

TRUNCATE TABLE `<GCP_PROJECT_ID>.<BQ_DATASET>.principal_access_map`;

INSERT INTO `<GCP_PROJECT_ID>.<BQ_DATASET>.principal_access_map`
  (principal_uri, person_label, sees_all_rows)
VALUES
  ('principal://iam.googleapis.com/locations/global/workforcePools/<WORKFORCE_POOL_ID>/subject/<ANALYST_OBJECT_ID>',
   'Analyst (analyst@<TENANT_DOMAIN>)', FALSE),
  ('principal://iam.googleapis.com/locations/global/workforcePools/<WORKFORCE_POOL_ID>/subject/<M365_ADMIN_OBJECT_ID>',
   'M365 Admin (m365-admin@<TENANT_DOMAIN>) -- OID PLACEHOLDER', TRUE);
