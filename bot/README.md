# Server Access Bot

Telegram bot that replaces the server-access Google Form. It keeps the user's
profile separate from server requests, provisions Unix accounts through the
existing root-owned `hermes-add-user` helpers, stores credentials encrypted in
SQLite, and mirrors successful applications to the existing Google Sheet.

## User flow

1. `/start` replaces any cached legacy keyboard with the current menu: profile,
   `My Info`, available servers, saved servers, access request, and rules.
2. On first contact, the bot searches the legacy Google Sheet by normalized
   Telegram username. One-tap confirmation is offered only when the row already
   contains the same numeric Telegram user ID. Legacy username-only rows never
   reveal personal data and require a separate trusted migration.
3. The confirmed legacy identity is bound to the immutable numeric Telegram
   user ID. Duplicate rows are merged into a set of registered services.
4. Both forms keep a per-user draft in Telegram CloudStorage. Saved profiles can
   also be edited one field at a time in chat without re-entering other fields.
5. `My Info` combines profile, Unix username, and registered services in one
   message. `Мои серверы` additionally shows credentials created by the bot.
6. The user opens `Получить доступ`, selects managed servers, and enters only
   the Unix username, access term, and optional activity.
7. The bot validates the untrusted Web App payload and shows the request
   summary in chat. After confirmation, it checks the username on every
   requested server.
8. The bot provisions the accounts and sends the credentials directly to the
   same Telegram numeric user ID.
9. `Мои серверы` always lists the complete catalog. Imported resources show
   the Unix username but explicitly state that their old password is not stored.
   The Yandex server links to `@aloeme` and is never provisioned by this bot.
10. Credential messages are protected from forwarding and deleted after five
   minutes.
11. A later request only provisions newly selected servers. Existing passwords
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
