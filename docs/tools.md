# Google Workspace MCP Tools

Stage 1 implements Google Sheets and lead management tools. All tools return structured JSON with `ok`, `action`, `resource`, `dryRun`, `summary`, `warnings` and optional `data`.

## Google Sheets

### `google_sheets_get_spreadsheet`

Input:

```json
{ "spreadsheetId": "..." }
```

Reads spreadsheet metadata.

### `google_sheets_list_tabs`

Input:

```json
{ "spreadsheetId": "..." }
```

Returns sheet/tab properties.

### `google_sheets_read_range`

Input:

```json
{ "spreadsheetId": "...", "range": "Leads!A1:M20" }
```

Reads values from an A1 range.

### `google_sheets_append_row`

Input:

```json
{
  "spreadsheetId": "...",
  "sheetName": "Leads",
  "values": ["Alice", "alice@example.com"],
  "dryRun": true
}
```

Appends one row. When `dryRun=true`, no Google write is executed.

### `google_sheets_update_range`

Input:

```json
{
  "spreadsheetId": "...",
  "range": "Leads!A2:M2",
  "values": [["Alice", "contacted"]],
  "dryRun": false,
  "requiresConfirmation": true
}
```

Dangerous write gate: non-dry-run updates require `requiresConfirmation=true`.

### `google_sheets_search_rows`

Input:

```json
{
  "spreadsheetId": "...",
  "sheetName": "Leads",
  "headerRow": 1,
  "filters": { "email": "alice@example.com" }
}
```

Searches rows by exact field/value match using the header row.

### `google_sheets_create_spreadsheet`

Input:

```json
{ "title": "Leads", "dryRun": true }
```

Creates a spreadsheet. Supports dry-run.

### `google_sheets_create_tab`

Input:

```json
{ "spreadsheetId": "...", "title": "Leads", "dryRun": true }
```

Creates a sheet/tab. Supports dry-run.

### `google_sheets_upsert_lead`

Input:

```json
{
  "spreadsheetId": "...",
  "sheetName": "Leads",
  "lead": {
    "name": "Alice",
    "company": "Example Inc",
    "email": "alice@example.com",
    "phone": "+15551002000",
    "telegram": "alice",
    "website": "example.com",
    "source": "website",
    "status": "new",
    "notes": "Asked for pricing",
    "lastContactedAt": null,
    "nextFollowUpAt": null,
    "owner": "sales",
    "customFields": { "external_id": "abc-1" }
  },
  "dedupeKeys": ["email", "phone", "telegram", "website", "custom:external_id"],
  "dryRun": false
}
```

Creates or updates a lead row. If a duplicate is found, the existing row is updated and the new note is appended with a UTC timestamp.

### `google_sheets_update_lead_status`

Input:

```json
{
  "spreadsheetId": "...",
  "sheetName": "Leads",
  "leadIdentifier": { "email": "alice@example.com" },
  "status": "contacted",
  "note": "Called and left voicemail",
  "dryRun": false
}
```

Updates status and appends a timestamped note.

## Future Stages

- Stage 2: Google Calendar tools and follow-up scheduling.
- Stage 3: Gmail search/read/draft with send safety gate.
- Stage 4: Drive, Contacts and full hardening.
