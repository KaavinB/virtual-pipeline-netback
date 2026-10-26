# One entry point for everything. `make help` lists targets.
.DEFAULT_GOAL := help
SHELL := /bin/bash
UV := uv run
COMPOSE := docker compose
OSRM_IMAGE := ghcr.io/project-osrm/osrm-backend:v26.10.0-debian
OSRM_DIR := data/osrm

.PHONY: help setup up down reset migrate psql test lint ingest ingest-full dq pipeline sites routes osrm-local osrm-stop assumptions all sensitivity export dashboard screenshots site

help: ## list targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup: ## install Python deps into .venv (uv)
	uv sync

up: ## start Postgres and wait until healthy
	$(COMPOSE) up -d --wait db

down: ## stop Postgres (data kept in the docker volume)
	$(COMPOSE) down

reset: ## DESTROY the database (and Metabase) volumes and start fresh
	$(COMPOSE) --profile bi down -v
	$(MAKE) up migrate

migrate: ## apply pending SQL migrations
	$(UV) python -m ingestion.migrate

psql: ## open a psql shell inside the container
	$(COMPOSE) exec db psql -U $${PGUSER:-vpv} -d $${PGDATABASE:-vpv}

test: ## run pytest
	$(UV) pytest -q

lint: ## ruff lint + format check
	$(UV) ruff check . && $(UV) ruff format --check .

ingest: ## pull new EIA prices (30-day overlap, idempotent)
	$(UV) python -m ingestion.eia

ingest-full: ## backfill EIA prices from 2010
	$(UV) python -m ingestion.eia --full

dq: ## run data-quality checks -> ops.dq_results (fails on 'error' checks)
	$(UV) python -m ingestion.dq

pipeline: migrate ingest dq ## everything a scheduled refresh does

all: up migrate ingest-full sites assumptions routes dq sensitivity export ## build everything from an empty database

sites: ## select example sites from World Bank VIIRS flares -> dim_site
	$(UV) python -m ingestion.sites

routes: ## route every site to every delivery hub via OSRM (cached in fact_route)
	$(UV) python -m ingestion.routes $(ARGS)

osrm-local: ## self-hosted OSRM on :5001 (726 MB Texas-only extract; see docs/05)
	mkdir -p $(OSRM_DIR)
	test -f $(OSRM_DIR)/texas.osm.pbf || curl -L -o $(OSRM_DIR)/texas.osm.pbf \
		https://download.geofabrik.de/north-america/us/texas-latest.osm.pbf
	test -f $(OSRM_DIR)/texas.osrm.mldgr || ( \
		docker run --rm -v "$(CURDIR)/$(OSRM_DIR):/data" $(OSRM_IMAGE) osrm-extract -p /opt/car.lua /data/texas.osm.pbf && \
		docker run --rm -v "$(CURDIR)/$(OSRM_DIR):/data" $(OSRM_IMAGE) osrm-partition /data/texas.osrm && \
		docker run --rm -v "$(CURDIR)/$(OSRM_DIR):/data" $(OSRM_IMAGE) osrm-customize /data/texas.osrm )
	docker run -d --rm --name vpv-osrm -p 5001:5000 -v "$(CURDIR)/$(OSRM_DIR):/data" $(OSRM_IMAGE) \
		osrm-routed --algorithm mld /data/texas.osrm
	@echo "OSRM running on http://localhost:5001 - set OSRM_URL in .env, then: make routes ARGS=--refresh"

osrm-stop: ## stop the local OSRM container
	docker stop vpv-osrm

assumptions: ## validate assumptions.yaml and load it into core.assumption
	$(UV) python -m ingestion.assumptions

sensitivity: ## one-at-a-time tornado of netback -> mart.sensitivity_tornado
	$(UV) python -m ingestion.sensitivity

export: ## write dashboard extracts -> dashboards/exports/*.csv (for Tableau Public)
	$(UV) python -m ingestion.export

dashboard: ## start Metabase on :3000 and (re)build the dashboard from code
	$(COMPOSE) --profile bi up -d --wait metabase
	$(UV) python -m dashboards.metabase.provision

screenshots: ## README screenshots of the dashboard: make screenshots URL=<public dashboard link>
	uv run --with playwright==1.63.0 python -m dashboards.metabase.screenshots $(URL)

site: ## export site/data.json for the static GitHub Pages dashboard (commit + push to publish)
	$(UV) python -m dashboards.site.export
