# Server Access Bot

Telegram bot that replaces the server-access Google Form. It keeps the user's
profile separate from server requests, provisions Unix accounts through the
existing root-owned `hermes-add-user` helpers, stores credentials encrypted in
SQLite, and mirrors successful applications to the existing Google Sheet.

## User flow

1. `/start` replaces any cached legacy keyboard with the current six-button
   menu: profile, available servers, saved servers, access request, rules, and
   live server status. The buttons are arranged in three rows of two.
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
   add managed servers immediately. Every `mlsub` request and every new
   identity create a durable request that a configured admin approves or
   rejects with one Telegram button.
8. After approval, the bot provisions the accounts and sends the credentials
   directly to the applicant's immutable Telegram numeric user ID. Requesting
   `mlsub` requires an already approved BrainLab account. After the separate
   approval, a root-owned helper enables the protected `mlsub-students` group.
9. `Мои серверы` always lists the complete catalog. Imported resources show
   the Unix username but explicitly state that their old password is not stored.
   Link previews are disabled, so Telegram does not attach contact cards.
   The Yandex server is temporarily removed from the catalog and monitoring;
   imported `yandex` registry rows are kept only for Google Sheet round-trips.
10. Credential messages are protected from forwarding and remain available in
    the private chat until the user deletes them.
11. A later request only provisions newly selected servers. Existing passwords
   are never rotated.
12. `/demo` is restricted to `ACCESS_BOT_DEMO_USER_IDS` and exercises both
    forms with fake credentials without changing SQLite, Sheets, or Unix users.
13. A read-only background monitor checks the managed GPU servers every
    five minutes. It notifies a linked Telegram user only after that user's
    processes hold at least 4 GB VRAM on a GPU with at most 2% utilization for
    at least 30 minutes. The bot never stops processes automatically and sends
    at most one reminder per 12 hours. Unknown or ambiguous Unix usernames are
    reported only to the configured admins. A linked owner can explicitly stop
    the exact reported processes after confirmation. Action buttons do not
    expire, but PID ownership and process start time are rechecked before every
    signal.
14. `Статус серверов` runs a fresh parallel `nvidia-smi` snapshot. It shows
    every GPU's utilization and VRAM usage and marks unreachable SSH hosts.
15. A disk monitor checks `/`, `/home`, and inode availability every five minutes.
    Configured bot admins receive a private warning below 15% free and a critical
    alert below 5% free. Alerts include growth since the previous sample and over
    the last 24 hours. A root-owned helper reads the five largest users from ZFS
    metadata or filesystem quotas without walking all files. For a low root
    filesystem it performs only allowlisted system-cache cleanup and reports the
    measured before/after result.

## H200 access order

1. Send full name and MIPT email to `@undef1ne5` and request VPN for H200.
2. Wait for the email from `helpdesk02@vkusvill.ru`.
3. Submit the access request through `@brainlab_server_access_bot`. No separate administrator message is required.

Other managed servers do not require this preliminary VPN step.

## GPU idle monitor

The monitor uses only read-only SSH commands (`nvidia-smi` and `ps`). Its
observation counters and notification cooldowns are stored in the same SQLite
database, so a bot restart does not forget an already observed idle process.
All thresholds are configurable through the `GPU_MONITOR_*` and `GPU_IDLE_*`
environment variables documented in `.env.example`.

## Server chats and disk monitoring

Add the bot to a server group or forum topic and run one of these commands as a
configured bot admin:

```text
/bind_server shkodnik_opt
/bind_server brain_lab
/bind_server vv_h200
/bind_server 2_a100_brain
```

The binding stores both the Telegram chat ID and the current forum topic ID for
future group delivery. Current automatic alerts go privately to configured bot
admins. `/unbind_server <host>` removes a binding. `/disk_status` requests a
fresh disk-space snapshot without waiting for an alert.

Disk inspection uses `/usr/local/sbin/hermes-disk-report`. The SSH account may
run only its validated `summary`, `top-home`, and `cleanup-root` modes through
`sudo`; arbitrary paths and shell commands are not accepted. Install
`deploy/hermes-disk-cleanup.conf` into `/etc/tmpfiles.d/` for the optional
14-day `/tmp` cleanup policy. ZFS capacity includes child datasets instead of
using the misleading parent-dataset `df` size. On the ext4 BrainLab `/home`,
`top-home` reads the completed cache produced by `hermes-home-scan`; install and
enable `deploy/hermes-fast-du`, `deploy/hermes-home-scan`,
`deploy/hermes-home-scan.service`, and `deploy/hermes-home-scan.timer` to refresh
that cache once per day with idle I/O priority. The scanner writes a root-owned
regular `latest.tsv` atomically; the report helper rejects stale, oversized, or
non-root-owned cache files.

## User documentation

Install `deploy/user-docs/BRAINLAB_RULES.md` and the host-specific
`deploy/user-docs/<host>/SERVER_GUIDE.md` into `/etc/skel` before enabling
provisioning. New accounts receive exactly these two Markdown files through
`useradd -m`. On BrainLab, `hermes-enable-mlsub` restores either missing file as
the target Unix user before adding that user to `mlsub-students`; it never
overwrites an existing file.

## Local verification

```bash
uv sync --python 3.12
uv run pytest -q
uv run python -m compileall -q server_access_bot
```

## Deployment

Production runs on do-vpn from a plain clone of this repository at
`/opt/ssh_websile` (read-only deploy key `/root/.ssh/ssh_websile_deploy`).
The service runs `bot/` from that clone with the virtualenv
`/opt/server-access-bot/.venv`. Never edit files on the server: change the
code here, merge the pull request, then deploy:

```bash
ssh do-vpn 'cd /opt/ssh_websile && git pull --ff-only && systemctl restart server-access-bot'
```

`git -C /opt/ssh_websile status` must stay clean. A dirty tree means someone
edited production directly; commit that work here before the next pull.

## Runtime secrets

The deployment reads `/etc/server-access-bot/bot.env` and a Google service
account JSON file. Both must be root-owned with mode `0600`. Generate the
database encryption key with:

```bash
python3 -c 'import base64, secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'
```

Never commit the bot token, encryption key, service-account key, SQLite
database, or credential-bearing logs.


## Lab Knowledge MCP access

The **База знаний MCP** button and `/mcp` explain the shared knowledge base and
link to the public Atlas with fictional data. **Подать заявку** sends an application
with the person's name and Telegram ID directly to the owner; no project description
is requested. `MCP_ADMIN_USER_IDS` controls approval independently of server provisioning.
After approval the bot creates a `member` identity bound to the immutable numeric
Telegram ID and sends its token, onboarding guide, Atlas and the configured testing
group. SSH/server access is not required.

The applicant's key is encrypted in the existing SQLite database. The remote
bridge receives it on stdin, stores only its hash in MCP, and verifies the owner
and role before returning. Retrying after an SSH timeout or failed delivery reuses
the same identity and token. Existing elevated or disabled accounts require manual
review. Neither the request nor its purpose grants access to somebody else's project.

Owner commands:

- `/mcp_requests`: pending approvals and unfinished deliveries, including after restart.
- `/mcp_revoke TELEGRAM_ID`: revoke that bot-issued token.

Participants can redisplay their connection or revoke their own token via `/mcp`.
Group callbacks and approval attempts by other users do not grant access.

Deploy `deploy/telegram_mcp_access.py` to the canonical MCP service's `scripts/`
and `deploy/brainlab-mcp-access` to its owner's `~/.local/bin/`. The wrapper reuses
that service's environment and current Python runtime. No backend restart is needed.
The bot's existing SSH identity reaches that wrapper. Configure `MCP_ADMIN_USER_IDS`
and `MCP_TESTING_GROUP_URL` only in `/etc/server-access-bot/bot.env` (mode 0600).
Do not put the private invite or tokens in the repository.

Verification: `tests/test_mcp_access.py` exercises the real SQLite store and aiogram
router with a fake Telegram transport. `deploy/test_telegram_mcp_access.py` runs in
the MCP Python environment against a disposable `TEST_DATABASE_URL` with `_test_`
in its database name. It tests token replay/revocation and the published MCP tools
for member, viewer, contributor and project-creator permissions. It must never
run against the production database.
