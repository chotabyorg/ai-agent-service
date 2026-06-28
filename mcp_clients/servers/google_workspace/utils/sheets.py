"""Google Sheets A1 notation helpers."""

import re


_SHEET_NAME_RE = re.compile(r"^[A-Za-z0-9 _.-]{1,100}$")
_A1_RE = re.compile(
    r"^(?:(?P<sheet>'(?:[^']|'')+'|[A-Za-z0-9 _.-]+)!)?"
    r"(?P<start_col>[A-Z]{1,3})(?P<start_row>[1-9][0-9]*)"
    r"(?::(?P<end_col>[A-Z]{1,3})(?P<end_row>[1-9][0-9]*))?$"
)


def quote_sheet_name(sheet_name: str) -> str:
    """Quote a sheet name for A1 notation."""

    clean_name = sheet_name.strip()
    if not clean_name:
        raise ValueError("Sheet name cannot be empty.")
    if not _SHEET_NAME_RE.match(clean_name):
        raise ValueError("Sheet name contains unsupported characters.")
    return "'" + clean_name.replace("'", "''") + "'"


def build_sheet_range(sheet_name: str, start: str, end: str | None = None) -> str:
    """Build a sheet-qualified A1 range."""

    cell_range = start.upper() if end is None else f"{start.upper()}:{end.upper()}"
    parse_a1_range(cell_range)
    return f"{quote_sheet_name(sheet_name)}!{cell_range}"


def build_full_rows_range(sheet_name: str, header_row: int = 1) -> str:
    """Build a broad row range used for lead tables."""

    if header_row < 1:
        raise ValueError("header_row must be >= 1.")
    return f"{quote_sheet_name(sheet_name)}!A{header_row}:ZZ"


def parse_a1_range(range_name: str) -> dict[str, str | int | None]:
    """Validate and parse a simple A1 range."""

    value = range_name.strip()
    match = _A1_RE.match(value)
    if not match:
        raise ValueError("Invalid A1 range.")

    start_row = int(match.group("start_row"))
    end_row = int(match.group("end_row")) if match.group("end_row") else None
    if end_row is not None and end_row < start_row:
        raise ValueError("Range end row must be greater than or equal to start row.")

    return {
        "sheet": match.group("sheet"),
        "start_col": match.group("start_col"),
        "start_row": start_row,
        "end_col": match.group("end_col"),
        "end_row": end_row,
    }
