# Google Workspace MCP Tools

Stages 1-4 implement Google Sheets, lead management, Google Calendar, Gmail, Drive and Contacts tools. All tools return structured JSON with `ok`, `action`, `resource`, `dryRun`, `summary`, `warnings` and optional `data`.

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

## Google Calendar

### `google_calendar_list_calendars`

Input:

```json
{}
```

Lists calendars visible to the authorized account.

### `google_calendar_search_events`

Input:

```json
{
  "query": "Alice",
  "timeMin": "2026-06-01T00:00:00Z",
  "timeMax": "2026-06-30T23:59:59Z",
  "calendarId": "primary",
  "maxResults": 20
}
```

Searches events with optional query and time bounds.

### `google_calendar_create_event`

Input:

```json
{
  "calendarId": "primary",
  "title": "Discovery call",
  "description": "Discuss proposal",
  "start": "2026-06-01T12:00:00Z",
  "end": "2026-06-01T12:30:00Z",
  "attendees": [{ "email": "alice@example.com", "displayName": "Alice" }],
  "location": "Google Meet",
  "leadContext": { "name": "Alice", "email": "alice@example.com" },
  "dryRun": true
}
```

Creates an event. Supports dry-run and appends compact lead context to the description.

### `google_calendar_update_event`

Input:

```json
{
  "calendarId": "primary",
  "eventId": "event-id",
  "title": "Updated title",
  "dryRun": false,
  "requiresConfirmation": true
}
```

Updates an event. Non-dry-run updates require `requiresConfirmation=true`.

### `google_calendar_delete_event`

Input:

```json
{
  "calendarId": "primary",
  "eventId": "event-id",
  "dryRun": false,
  "requiresConfirmation": true
}
```

Deletes an event only when `requiresConfirmation=true` and `ENABLE_DESTRUCTIVE_TOOLS=true`.

### `google_calendar_create_followup`

Input:

```json
{
  "calendarId": "primary",
  "lead": {
    "name": "Alice",
    "company": "Example Inc",
    "email": "alice@example.com",
    "status": "interested"
  },
  "followUpAt": "2026-06-01T12:00:00Z",
  "description": "Follow up on pricing",
  "durationMinutes": 30,
  "dryRun": true
}
```

Creates a lead follow-up reminder as a Calendar event.

## Gmail

### `gmail_search_emails`

Input:

```json
{
  "query": "from:alice@example.com newer_than:30d",
  "maxResults": 10
}
```

Searches Gmail messages. Requires `https://www.googleapis.com/auth/gmail.readonly`.

### `gmail_read_thread`

Input:

```json
{ "threadId": "thread-id" }
```

Reads thread metadata and snippets without returning full message bodies. Requires `gmail.readonly`.

### `gmail_create_draft`

Input:

```json
{
  "to": ["alice@example.com"],
  "cc": [],
  "bcc": [],
  "subject": "Proposal",
  "body": "Hello Alice",
  "leadContext": { "name": "Alice", "email": "alice@example.com" },
  "dryRun": true
}
```

Creates a Gmail draft. It does not send email. Requires `gmail.compose` for non-dry-run calls.

### `gmail_send_draft`

Input:

```json
{
  "draftId": "draft-id",
  "requiresConfirmation": true
}
```

Sends an existing Gmail draft only when all gates pass:

- `requiresConfirmation=true`
- `ENABLE_GMAIL_SEND=true`
- OAuth includes `https://www.googleapis.com/auth/gmail.send`

### `gmail_reply_to_thread`

Input:

```json
{
  "threadId": "thread-id",
  "body": "Thanks, following up here.",
  "to": [],
  "cc": [],
  "bcc": [],
  "subject": null,
  "leadContext": { "name": "Alice", "email": "alice@example.com" },
  "dryRun": true,
  "requiresConfirmation": true
}
```

Creates a reply draft in the existing thread. It does not send email. If `to` is omitted, the tool tries to infer the recipient from the thread's latest `From` header.

## Google Drive

### `google_drive_search_files`

Input:

```json
{
  "query": "Alice",
  "mimeType": "application/pdf",
  "maxResults": 10
}
```

Searches non-trashed Drive files by name and optional MIME type. Requires `drive.metadata.readonly`.

### `google_drive_create_folder`

Input:

```json
{
  "name": "Lead Reports",
  "parentFolderId": null,
  "dryRun": true
}
```

Creates a Drive folder. Requires `drive.file` for non-dry-run calls.

### `google_drive_create_text_file`

Input:

```json
{
  "name": "lead-report.json",
  "content": "{\"ok\": true}",
  "mimeType": "application/json",
  "parentFolderId": null,
  "dryRun": true
}
```

Creates a small UTF-8 text, JSON, CSV or Markdown file. Content is capped at 1 MB. Requires `drive.file` for non-dry-run calls.

## Google Contacts

### `google_contacts_search`

Input:

```json
{
  "query": "alice@example.com",
  "maxResults": 10
}
```

Searches Google Contacts through the People API. Requires `contacts`.

### `google_contacts_upsert_contact`

Input:

```json
{
  "contact": {
    "name": "Alice Example",
    "company": "Example Inc",
    "email": "alice@example.com",
    "phone": "+15551002000",
    "telegram": "alice",
    "website": "example.com",
    "notes": "Qualified lead",
    "customFields": { "external_id": "abc-1" }
  },
  "dedupeKeys": ["email", "phone", "telegram", "website", "custom:external_id"],
  "dryRun": true
}
```

Creates or updates a Google Contact. In dry-run mode, it only returns the People API payload that would be used.
