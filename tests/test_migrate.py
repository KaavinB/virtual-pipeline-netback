import pytest

from ingestion.migrate import Migration, discover, pending


def write(tmp_path, name, sql="SELECT 1;"):
    (tmp_path / name).write_text(sql)


def test_discover_orders_by_version(tmp_path):
    write(tmp_path, "002_b.sql")
    write(tmp_path, "001_a.sql")
    assert [m.version for m in discover(tmp_path)] == [1, 2]


def test_discover_rejects_bad_filename(tmp_path):
    write(tmp_path, "1_missing_padding.sql")
    with pytest.raises(ValueError, match="bad migration filename"):
        discover(tmp_path)


def test_discover_rejects_duplicate_versions(tmp_path):
    write(tmp_path, "001_a.sql")
    write(tmp_path, "001_b.sql")
    with pytest.raises(ValueError, match="duplicate"):
        discover(tmp_path)


def test_pending_skips_applied_and_detects_edits():
    m1, m2 = Migration(1, "001_a.sql", "A"), Migration(2, "002_b.sql", "B")
    assert pending([m1, m2], {1: m1.checksum}) == [m2]
    with pytest.raises(RuntimeError, match="modified after being applied"):
        pending([m1, m2], {1: "stale-checksum"})


def test_repo_migrations_are_well_formed():
    assert discover()[0].name == "001_schemas.sql"
