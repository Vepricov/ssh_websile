# Server Access Bot

Telegram bot that replaces the server-access Google Form. It keeps the user's
profile separate from server requests, provisions Unix accounts through the
existing root-owned `hermes-add-user` helpers, stores credentials encrypted in
SQLite, and mirrors successful applications to the existing Google Sheet.

## User flow

1. `/start` replaces any cached legacy keyboard with the current five-button
   menu: profile, available servers, saved servers, access request, and rules.
2. On first contact, the bot searches the legacy Google Sheet by normalized
   Telegram username. One-tap confirmation is offered only when the row already
   contains the same numeric Telegram user ID. Legacy username-only rows never
   reveal personal data and require a separate trusted migration.
3. The confirmed legacy identity is bound to the immutable numeric Telegram
   user ID. Duplicate rows are merged into a set of registered services.
4. Both forms keep a per-user draft in Telegram CloudStorage. Saved profiles can
   also be edited one field at a time in chat without re-entering other fields.
5. `Мой профиль` combines profile, Unix username, and registered services in one
   message. The legacy `/myinfo` command opens the same view. `Мои серверы`
   additionally shows credentials created by the bot.
6. The user opens `Получить доступ`, selects managed servers or the separate
   `mlsub / Cloud.ru` service, and enters only the Unix username, access term,
   and optional activity.
7. The bot validates the untrusted Web App payload. Known registry users can
   add resources immediately; a new identity creates a durable request that a
   configured admin approves or rejects with one Telegram button.
8. After approval, the bot provisions the accounts and sends the credentials
   directly to the applicant's immutable Telegram numeric user ID. Requesting
   `mlsub` first ensures a BrainLab account and then enables the protected
   `mlsub-students` group through a separate root-owned helper.
9. `Мои серверы` always lists the complete catalog. Imported resources show
   the Unix username but explicitly state that their old password is not stored.
   The Yandex server links to `@aloeme` and is never provisioned by this bot.
   Link previews are disabled, so Telegram does not attach the contact card.
10. Credential messages are protected from forwarding and deleted after five
   minutes.
11. A later request only provisions newly selected servers. Existing passwords
   are never rotated.
12. `/demo` is restricted to `ACCESS_BOT_DEMO_USER_IDS` and exercises both
    forms with fake credentials without changing SQLite, Sheets, or Unix users.
13. A read-only background monitor checks the three managed GPU servers every
    five minutes. It notifies a linked Telegram user only after that user's
    processes hold at least 4 GB VRAM on a GPU with at most 2% utilization for
    at least 30 minutes. The bot never stops processes automatically and sends
    at most one reminder per 12 hours. Unknown or ambiguous Unix usernames are
    reported only to the configured admins.

## H200 access order

1. Send full name and MIPT email to `@undef1ne5` and request VPN for H200.
2. Wait for the email from `helpdesk02@vkusvill.ru`.
3. Submit the access request and notify `@shkodnik1917`.

Other managed servers do not require this preliminary VPN step.

## GPU idle monitor

The monitor uses only read-only SSH commands (`nvidia-smi` and `ps`). Its
observation counters and notification cooldowns are stored in the same SQLite
database, so a bot restart does not forget an already observed idle process.
All thresholds are configurable through the `GPU_MONITOR_*` and `GPU_IDLE_*`
environment variables documented in `.env.example`.

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
