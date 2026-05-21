# Google OAuth Setup

This document covers Google OAuth setup for `chota-google-workspace-mcp`.

## Google Cloud Console

1. Open Google Cloud Console.
2. Create or select a project for chota Google Workspace integrations.
3. Enable APIs:
   - Google Sheets API
   - Google Calendar API
4. Configure the OAuth consent screen.
5. Add test users for local development if the app is not published.
6. Create OAuth credentials with type `Web application`.
7. Add the exact authorized redirect URI used in `GOOGLE_REDIRECT_URI`.

## Scopes

Use least privilege:

```text
https://www.googleapis.com/auth/spreadsheets
https://www.googleapis.com/auth/calendar
```

Gmail, Drive and Contacts scopes are intentionally not required until later stages.

## Environment

```env
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
GOOGLE_REDIRECT_URI=http://localhost:8080/oauth2/callback
GOOGLE_REFRESH_TOKEN=
GOOGLE_SCOPES=https://www.googleapis.com/auth/spreadsheets https://www.googleapis.com/auth/calendar
GOOGLE_TOKEN_STORAGE_MODE=local
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
- Do not request Gmail send, Drive or Contacts scopes for Sheets-only agents.
- Rotate refresh tokens when an agent or workspace is disconnected.
- Keep logs free of tokens, client secrets and personal data.
