import pytest

from ingestion.assumptions import load, validate


def entry(**overrides):
    base = {"key": "k", "value": 1.0, "unit": "usd_per_mmbtu", "basis": "rough_estimate",
            "source": "judgment"}  # fmt: skip
    return {**base, **overrides}


def test_repo_file_is_valid_and_every_cost_has_a_source():
    entries = load()
    assert len(entries) > 20
    assert all(e["source"] for e in entries)


def test_rejects_value_outside_its_sensitivity_range():
    with pytest.raises(ValueError, match="outside"):
        validate([entry(low=2.0, high=3.0)])


def test_sourced_number_needs_a_url():
    with pytest.raises(ValueError, match="needs a url"):
        validate([entry(basis="sourced")])


def test_rejects_duplicates_unknown_basis_and_unknown_fields():
    with pytest.raises(ValueError) as err:
        validate([entry(), entry(basis="vibes", colour="red")])
    message = str(err.value)
    assert "duplicate key" in message and "basis must be" in message and "unknown fields" in message


def test_old_dollar_year_needs_its_cpi():
    with pytest.raises(ValueError, match="cpi_u_2015 is not defined"):
        validate([entry(usd_year=2015)])
    validate([entry(usd_year=2015), entry(key="cpi_u_2015", value=237.017, unit="index")])
