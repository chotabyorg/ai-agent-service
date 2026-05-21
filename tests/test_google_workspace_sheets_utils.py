import pytest

from mcp_clients.servers.google_workspace.utils.sheets import (
    build_full_rows_range,
    build_sheet_range,
    parse_a1_range,
    quote_sheet_name,
)


def test_parse_a1_range_accepts_sheet_qualified_range() -> None:
    parsed = parse_a1_range("Leads!A2:C10")

    assert parsed["sheet"] == "Leads"
    assert parsed["start_col"] == "A"
    assert parsed["start_row"] == 2
    assert parsed["end_col"] == "C"
    assert parsed["end_row"] == 10


def test_parse_a1_range_rejects_reversed_rows() -> None:
    with pytest.raises(ValueError):
        parse_a1_range("A10:B2")


def test_build_sheet_range_quotes_tab_name() -> None:
    assert build_sheet_range("Lead Sheet", "a1", "b2") == "'Lead Sheet'!A1:B2"


def test_quote_sheet_name_rejects_unsafe_name() -> None:
    with pytest.raises(ValueError):
        quote_sheet_name("Leads!A1")


def test_build_full_rows_range() -> None:
    assert build_full_rows_range("Leads", 2) == "'Leads'!A2:ZZ"
