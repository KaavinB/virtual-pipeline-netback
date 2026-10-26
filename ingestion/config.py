"""Settings loaded once from the environment (.env in development, secrets in CI)."""

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    eia_api_key: str
    osrm_url: str
    pg_dsn: str


def get_settings() -> Settings:
    return Settings(
        eia_api_key=os.getenv("EIA_API_KEY", ""),
        osrm_url=os.getenv("OSRM_URL", "https://router.project-osrm.org").rstrip("/"),
        pg_dsn=(
            f"host={os.getenv('PGHOST', 'localhost')} port={os.getenv('PGPORT', '5434')} "
            f"user={os.getenv('PGUSER', 'vpv')} password={os.getenv('PGPASSWORD', 'vpv')} "
            f"dbname={os.getenv('PGDATABASE', 'vpv')}"
        ),
    )


def setup_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
