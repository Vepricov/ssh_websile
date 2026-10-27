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
    ipa_admin_password: str = ""
    ascend_group_url: str = ""
    alibaba_requests_open: bool = True
    closed_servers: frozenset[str] = frozenset()
    demo_user_ids: frozenset[int] = frozenset()
    admin_user_ids: frozenset[int] = frozenset()
    mcp_admin_user_ids: frozenset[int] = frozenset()
    mcp_testing_group_url: str = ""
    gpu_monitor_enabled: bool = True
    gpu_monitor_interval_seconds: int = 300
    gpu_idle_memory_threshold_mb: int = 4096
    gpu_idle_utilization_threshold_percent: int = 2
    gpu_idle_minimum_duration_seconds: int = 1800
    gpu_idle_minimum_samples: int = 6
    gpu_idle_notification_cooldown_seconds: int = 43200
    disk_monitor_enabled: bool = True
    disk_monitor_interval_seconds: int = 300
    disk_warning_free_percent: int = 15
    disk_critical_free_percent: int = 5
    disk_notification_cooldown_seconds: int = 21600

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
                "1S_0e24zqR_vadGb6GpJhL_BxY8Vw9pLudT-GEiwmqP8",
            ),
            sheet_id=int(os.environ.get("GOOGLE_SHEET_ID", "1269070972")),
            ipa_admin_password=os.environ.get("IPA_ADMIN_PASSWORD", ""),
            ascend_group_url=os.environ.get("ASCEND_GROUP_URL", "").strip(),
            alibaba_requests_open=os.environ.get(
                "ALIBABA_REQUESTS_OPEN", "true"
            ).lower()
            in {"1", "true", "yes", "on"},
            closed_servers=frozenset(
                value.strip()
                for value in os.environ.get("CLOSED_SERVERS", "").split(",")
                if value.strip()
            ),
            demo_user_ids=demo_user_ids,
            admin_user_ids=admin_user_ids,
            mcp_admin_user_ids=frozenset(
                int(value.strip())
                for value in os.environ.get("MCP_ADMIN_USER_IDS", "").split(",")
                if value.strip()
            ),
            mcp_testing_group_url=os.environ.get("MCP_TESTING_GROUP_URL", "").strip(),
            gpu_monitor_enabled=os.environ.get(
                "GPU_MONITOR_ENABLED", "true"
            ).lower()
            in {"1", "true", "yes", "on"},
            gpu_monitor_interval_seconds=int(
                os.environ.get("GPU_MONITOR_INTERVAL_SECONDS", "300")
            ),
            gpu_idle_memory_threshold_mb=int(
                os.environ.get("GPU_IDLE_MEMORY_THRESHOLD_MB", "4096")
            ),
            gpu_idle_utilization_threshold_percent=int(
                os.environ.get("GPU_IDLE_UTILIZATION_THRESHOLD_PERCENT", "2")
            ),
            gpu_idle_minimum_duration_seconds=int(
                os.environ.get("GPU_IDLE_MINIMUM_DURATION_SECONDS", "1800")
            ),
            gpu_idle_minimum_samples=int(
                os.environ.get("GPU_IDLE_MINIMUM_SAMPLES", "6")
            ),
            gpu_idle_notification_cooldown_seconds=int(
                os.environ.get("GPU_IDLE_NOTIFICATION_COOLDOWN_SECONDS", "43200")
            ),
            disk_monitor_enabled=os.environ.get(
                "DISK_MONITOR_ENABLED", "true"
            ).lower()
            in {"1", "true", "yes", "on"},
            disk_monitor_interval_seconds=int(
                os.environ.get("DISK_MONITOR_INTERVAL_SECONDS", "300")
            ),
            disk_warning_free_percent=int(
                os.environ.get("DISK_WARNING_FREE_PERCENT", "15")
            ),
            disk_critical_free_percent=int(
                os.environ.get("DISK_CRITICAL_FREE_PERCENT", "5")
            ),
            disk_notification_cooldown_seconds=int(
                os.environ.get("DISK_NOTIFICATION_COOLDOWN_SECONDS", "21600")
            ),
        )
