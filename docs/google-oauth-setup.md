# Google OAuth Setup

This document covers Google OAuth setup for `chota-google-workspace-mcp`.

## Google Cloud Console

1. Open Google Cloud Console.
2. Create or select a project for chota Google Workspace integrations.
3. Enable APIs:
   - Google Sheets API
   - Google Calendar API
   - Gmail API
   - Google Drive API
   - People API
4. Configure the OAuth consent screen.
5. Add test users for local development if the app is not published.
6. Create OAuth credentials with type `Web application`.
7. Add the exact authorized redirect URI used in `GOOGLE_REDIRECT_URI`.

## Scopes

Use least privilege:

```text
https://www.googleapis.com/auth/spreadsheets
https://www.googleapis.com/auth/calendar
https://www.googleapis.com/auth/gmail.readonly
https://www.googleapis.com/auth/gmail.compose
https://www.googleapis.com/auth/drive.file
https://www.googleapis.com/auth/drive.metadata.readonly
https://www.googleapis.com/auth/contacts
```

Only add this scope for accounts that are allowed to send email through agents:

```text
https://www.googleapis.com/auth/gmail.send
```

For agents that do not need Drive search, omit `drive.metadata.readonly`. For agents that only create app-owned Drive files, keep only `drive.file`.

## Environment

```env
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
GOOGLE_REDIRECT_URI=http://localhost:8080/oauth2/callback
GOOGLE_REFRESH_TOKEN=
GOOGLE_SCOPES=https://www.googleapis.com/auth/spreadsheets https://www.googleapis.com/auth/calendar https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.compose https://www.googleapis.com/auth/drive.file https://www.googleapis.com/auth/drive.metadata.readonly https://www.googleapis.com/auth/contacts
GOOGLE_TOKEN_STORAGE_MODE=local
ENABLE_GMAIL_SEND=false
```

In production, store refresh tokens in encrypted platform credentials, not in local files.

## Refresh Token

Generate a refresh token through a trusted local OAuth helper or Google OAuth Playground configured with your OAuth client.

Requirements:

- request `access_type=offline`;
- request `prompt=consent` when a refresh token is needed again;
- store only the refresh token and OAuth client metadata in encrypted agent credentials.

## Security Notes

- Do not commit OAuth credentials or tokens.
- Do not request Gmail send, Drive or Contacts scopes for agents that do not need them.
- Keep `ENABLE_GMAIL_SEND=false` unless a production workflow has an explicit human-approved sending policy.
- Keep `ENABLE_DESTRUCTIVE_TOOLS=false` unless delete operations are explicitly approved.
- Rotate refresh tokens when an agent or workspace is disconnected.
- Keep logs free of tokens, client secrets and personal data.
