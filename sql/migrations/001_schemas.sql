-- Three schemas keep responsibilities apart:
--   core : the star schema (dimensions + facts), written only by ingestion
--   ops  : pipeline metadata - ingestion runs and data-quality results
--   mart : read-only views the dashboard consumes (business logic lives here, in SQL)
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS ops;
CREATE SCHEMA IF NOT EXISTS mart;
