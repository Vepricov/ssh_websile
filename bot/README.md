# Server Access Bot

Telegram bot that replaces the server-access Google Form. It collects the same
application fields, provisions Unix accounts through the existing root-owned
`hermes-add-user` helpers, stores credentials encrypted in SQLite, and mirrors
the application to the existing Google Sheet.

## User flow

1. The user opens a private chat with the bot.
2. The user opens the Telegram Mini App form, sees every field on one screen,
   selects the servers, reviews the answers, and sends them back to the bot.
3. The bot validates the untrusted Web App payload and shows a final summary.
4. After confirmation, the bot checks that the username is free on every
   requested server.
5. The bot provisions the accounts and sends the credentials directly to the
   same Telegram numeric user ID.
6. Credential messages are protected from forwarding and deleted after five
   minutes. The user can request them again through `Мои серверы`.
7. A later request only provisions newly selected servers. Existing passwords
   are never rotated.

## Local verification

```bash
uv sync --python 3.12
uv run pytest -q
uv run python -m compileall -q server_access_bot
```

## Runtime secrets

The deployment reads `/etc/server-access-bot/bot.env` and a Google service
account JSON file. Both must be root-owned with mode `0600`. Generate the
database encryption key with:

```bash
python3 -c 'import base64, secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'
```

Never commit the bot token, encryption key, service-account key, SQLite
database, or credential-bearing logs.
