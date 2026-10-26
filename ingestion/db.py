"""Thin wrapper around psycopg so every module connects the same way."""

import psycopg

from ingestion.config import get_settings


def connect(autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(get_settings().pg_dsn, autocommit=autocommit)
