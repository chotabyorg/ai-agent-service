# Lead Workflows

Stages 1-2 support lead workflows backed by Google Sheets and Google Calendar.

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

## Gmail

Gmail draft workflows are intentionally deferred:

- Stage 3: Gmail draft and sending safety gate.
