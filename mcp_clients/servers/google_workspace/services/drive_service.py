"""Google Drive helper logic."""

from __future__ import annotations


def escape_drive_query_value(value: str) -> str:
    """Escape a value used inside a Drive API query string."""

    return value.replace("\\", "\\\\").replace("'", "\\'")


def build_drive_search_query(query: str | None, mime_type: str | None) -> str:
    """Build a constrained Drive files.list query."""

    clauses = ["trashed = false"]
    if query:
        escaped = escape_drive_query_value(query.strip())
        clauses.append(f"name contains '{escaped}'")
    if mime_type:
        escaped_mime = escape_drive_query_value(mime_type.strip())
        clauses.append(f"mimeType = '{escaped_mime}'")
    return " and ".join(clauses)


def file_resource(file: dict) -> dict:
    """Return an audit-friendly Drive file resource."""

    return {
        "type": "google_drive_file",
        "fileId": file.get("id"),
        "name": file.get("name"),
        "mimeType": file.get("mimeType"),
    }
