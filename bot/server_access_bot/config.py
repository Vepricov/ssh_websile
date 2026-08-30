import base64
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    bot_token: str
    database_path: Path
    encryption_key: bytes
    google_service_account_file: Path
    spreadsheet_id: str
    sheet_id: int
    credential_message_ttl_seconds: int = 300
    demo_user_ids: frozenset[int] = frozenset()
    admin_user_ids: frozenset[int] = frozenset()

    @classmethod
    def from_env(cls) -> "Settings":
        key = base64.urlsafe_b64decode(os.environ["ACCESS_BOT_ENCRYPTION_KEY"])
        demo_user_ids = frozenset(
            int(value.strip())
            for value in os.environ.get("ACCESS_BOT_DEMO_USER_IDS", "").split(",")
            if value.strip()
        )
        admin_user_ids = frozenset(
            int(value.strip())
            for value in os.environ.get("ACCESS_BOT_ADMIN_USER_IDS", "").split(",")
            if value.strip()
        )
        return cls(
            bot_token=os.environ["ACCESS_BOT_TOKEN"],
            database_path=Path(
                os.environ.get(
                    "ACCESS_BOT_DATABASE", "/var/lib/server-access-bot/bot.sqlite3"
                )
            ),
            encryption_key=key,
            google_service_account_file=Path(
                os.environ.get(
                    "GOOGLE_SERVICE_ACCOUNT_FILE",
                    "/etc/server-access-bot/google-service-account.json",
                )
            ),
            spreadsheet_id=os.environ.get(
                "GOOGLE_SPREADSHEET_ID",
                "13wqQBDOwjVsuSc6nDctkUcjAs2BVIbmJd23kEVNbU98",
            ),
            sheet_id=int(os.environ.get("GOOGLE_SHEET_ID", "1269070972")),
            credential_message_ttl_seconds=int(
                os.environ.get("CREDENTIAL_MESSAGE_TTL_SECONDS", "300")
            ),
            demo_user_ids=demo_user_ids,
            admin_user_ids=admin_user_ids,
        )
