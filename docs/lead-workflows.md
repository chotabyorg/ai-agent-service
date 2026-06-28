# Lead Workflows

Stages 1-4 support lead workflows backed by Google Sheets, Google Calendar, Gmail drafts, Google Drive and Google Contacts.

## Lead Table

Recommended headers:

```text
name, company, email, phone, telegram, website, source, status, notes,
lastContactedAt, nextFollowUpAt, owner, customFields
```

If `google_sheets_upsert_lead` writes into an empty sheet, it writes the default header row first.

## New Lead Flow

1. Agent receives lead data.
2. Agent calls `google_sheets_upsert_lead` with `dryRun=true` if it needs to preview.
3. Agent calls `google_sheets_upsert_lead` with `dryRun=false`.
4. Server normalizes email, phone, Telegram username and website.
5. Server searches configured `dedupeKeys`.
6. If no duplicate exists, server appends a new row.

## Deduplication Flow

Supported dedupe keys:

- `email`
- `phone`
- `telegram`
- `website`
- `name`
- `company`
- `custom:<field>`

For custom fields, the sheet should store JSON in the `customFields` column.

## Existing Lead Flow

When a duplicate is found:

1. Existing row number is preserved.
2. Incoming non-empty lead fields replace empty or old values.
3. Incoming `notes` is appended with a UTC timestamp.
4. Response action is `lead_updated`.

## Status Update Flow

Use `google_sheets_update_lead_status` with one identifier:

```json
{
  "leadIdentifier": { "email": "alice@example.com" },
  "status": "contacted",
  "note": "Left voicemail"
}
```

Default lead statuses:

- `new`
- `contacted`
- `interested`
- `meeting_scheduled`
- `proposal_sent`
- `won`
- `lost`
- `do_not_contact`

Status values are currently validated as non-empty strings. A stricter enum can be enabled later if product requirements require it.

## Follow-up Flow

Use `google_calendar_create_followup` after lead creation or status update:

```json
{
  "lead": { "name": "Alice", "email": "alice@example.com" },
  "followUpAt": "2026-06-01T12:00:00Z",
  "durationMinutes": 30,
  "dryRun": false
}
```

The tool creates a Calendar event with lead context in the description and adds the lead email as an attendee when available.

## Gmail Draft Flow

Use `gmail_create_draft` after lead qualification:

```json
{
  "to": ["alice@example.com"],
  "subject": "Proposal",
  "body": "Hello Alice",
  "leadContext": { "name": "Alice", "email": "alice@example.com" },
  "dryRun": false
}
```

Sending is a separate action through `gmail_send_draft`. It is disabled by default and requires `ENABLE_GMAIL_SEND=true`, `requiresConfirmation=true`, and the `gmail.send` OAuth scope.

Use `gmail_reply_to_thread` to create a reply draft in an existing thread. It also does not send the email.

## Contact Sync Flow

Use `google_contacts_upsert_contact` after lead qualification:

```json
{
  "contact": {
    "name": "Alice Example",
    "company": "Example Inc",
    "email": "alice@example.com",
    "phone": "+15551002000"
  },
  "dedupeKeys": ["email", "phone"],
  "dryRun": false
}
```

The tool searches contacts by the first available dedupe value, updates the duplicate if found, or creates a new contact.

## Drive Report Flow

Use `google_drive_create_text_file` to store exported lead reports:

```json
{
  "name": "lead-report.json",
  "content": "{\"lead\":\"alice@example.com\"}",
  "mimeType": "application/json",
  "dryRun": false
}
```

Use `google_drive_search_files` to find documents related to a lead or company.
