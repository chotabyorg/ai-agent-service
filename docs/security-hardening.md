# Google Workspace MCP Security Hardening

The Google Workspace MCP server is designed for AI-agent use, where tool safety matters more than convenience.

## Scope Separation

Each feature validates only the scopes it needs:

- Sheets: `spreadsheets`
- Calendar: `calendar`
- Gmail search/read: `gmail.readonly`
- Gmail drafts/replies: `gmail.compose`
- Gmail send: `gmail.send`
- Drive create files/folders: `drive.file`
- Drive search: `drive.metadata.readonly`
- Contacts: `contacts`

Do not grant all scopes to every agent. Store scopes per agent credential record.

## Dangerous Actions

Protected actions:

- Gmail sending requires `ENABLE_GMAIL_SEND=true`, `requiresConfirmation=true`, and `gmail.send`.
- Calendar deletion requires `ENABLE_DESTRUCTIVE_TOOLS=true` and `requiresConfirmation=true`.
- Sheets range update requires `requiresConfirmation=true`.

## Dry Run

Write tools support dry-run where practical. Dry-run validates input and returns the payload that would be sent without calling Google APIs.

## Batching And Size Limits

- Search/list tools cap `maxResults`.
- Drive text upload content is capped at 1 MB.
- Email body and note fields are bounded by validation schemas.
- Recipient lists are bounded to avoid accidental bulk sending.

## Logging And Responses

Tool responses are structured and audit-friendly. They return resource identifiers, action names, summaries and warnings. Secrets and OAuth tokens are never returned.
