# Server Access Bot

Telegram bot that replaces the server-access Google Form. It keeps the user's
profile separate from server requests, provisions Unix accounts through the
existing root-owned `hermes-add-user` helpers, stores credentials encrypted in
SQLite, and mirrors successful applications to the existing Google Sheet.

## User flow

1. The user opens `Мой профиль`, enters their name and recommender, and sends
   the form directly to the bot.
2. The user opens `Добавиться на сервер`, selects managed servers, and enters
   only the Unix username, access term, and optional activity.
3. The bot validates both untrusted Web App payloads and shows the server
   request summary in chat.
4. After chat confirmation, the bot checks that the username is free on every
   requested server.
5. The bot provisions the accounts and sends the credentials directly to the
   same Telegram numeric user ID.
6. `Мои серверы` always lists the complete catalog. Managed resources show
   saved credentials or a missing-access state. The Yandex server links to
   `@aloeme` and is never provisioned by this bot.
7. Credential messages are protected from forwarding and deleted after five
   minutes.
8. A later request only provisions newly selected servers. Existing passwords
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
