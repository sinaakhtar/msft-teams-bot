-- 01_dataset.sql
-- Creates the demo dataset for the Microsoft Teams bot / BigQuery row-level
-- security demo.
--
-- Location choice: US multi-region.
--   Reason: no data-residency constraint applies to this synthetic demo data,
--   and the US multi-region is the BigQuery default, so the dataset lines up
--   with anything else created in <GCP_PROJECT_ID> without a cross-region join
--   error. Row-level security behaves identically in every location, so the
--   choice carries no functional weight here. If the demo is ever re-pointed
--   at EU-resident data, change this to `EU` and change --location=US in
--   apply.sh to match; the two must agree or bq will fail with a
--   "Not found: Dataset" error that actually means "wrong region".
--
-- Re-runnable: IF NOT EXISTS.

CREATE SCHEMA IF NOT EXISTS `<GCP_PROJECT_ID>.<BQ_DATASET>`
OPTIONS (
  location = 'US',
  description = 'Demo dataset for the Microsoft Teams bot -> ADK agent -> BigQuery row-level security walkthrough. Two federated Entra users query the same table and see different rows.'
);
