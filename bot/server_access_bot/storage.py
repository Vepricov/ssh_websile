import json
import os
import sqlite3
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .domain import (
    Application,
    GpuIdleCandidate,
    GpuIdleIncident,
    GpuProcessAction,
    ProcessIdentity,
    Profile,
    ProvisionedAccess,
    RegistryMatch,
)


class RegistryAlreadyClaimedError(ValueError):
    pass


class Storage:
    def __init__(self, path: Path, encryption_key: bytes) -> None:
        if len(encryption_key) not in (16, 24, 32):
            raise ValueError("encryption key must be 16, 24, or 32 bytes")
        self.path = path
        self.cipher = AESGCM(encryption_key)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    telegram_user_id INTEGER PRIMARY KEY,
                    telegram_username TEXT,
                    full_name TEXT NOT NULL,
                    server_username TEXT NOT NULL,
                    access_term TEXT NOT NULL,
                    recommender TEXT NOT NULL,
                    activity TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS profiles (
                    telegram_user_id INTEGER PRIMARY KEY,
                    telegram_username TEXT,
                    full_name TEXT NOT NULL,
                    recommender TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS registry_users (
                    telegram_user_id INTEGER PRIMARY KEY
                        REFERENCES profiles(telegram_user_id),
                    source_telegram_username TEXT NOT NULL,
                    server_username TEXT NOT NULL,
                    access_term TEXT NOT NULL,
                    activity TEXT NOT NULL,
                    source_row_count INTEGER NOT NULL,
                    confirmed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS registry_services (
                    telegram_user_id INTEGER NOT NULL
                        REFERENCES profiles(telegram_user_id),
                    host TEXT NOT NULL,
                    PRIMARY KEY (telegram_user_id, host)
                );
                CREATE TABLE IF NOT EXISTS accesses (
                    telegram_user_id INTEGER NOT NULL REFERENCES profiles(telegram_user_id),
                    host TEXT NOT NULL,
                    secret BLOB NOT NULL,
                    created INTEGER NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (telegram_user_id, host)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_user_id INTEGER NOT NULL,
                    event TEXT NOT NULL,
                    host TEXT,
                    detail TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS pending_requests (
                    request_id TEXT PRIMARY KEY,
                    telegram_user_id INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    resolved_at TEXT
                );
                CREATE TABLE IF NOT EXISTS gpu_idle_observations (
                    incident_key TEXT PRIMARY KEY,
                    host TEXT NOT NULL,
                    gpu_uuid TEXT NOT NULL,
                    gpu_index INTEGER NOT NULL,
                    server_username TEXT NOT NULL,
                    used_memory_mb INTEGER NOT NULL,
                    pids TEXT NOT NULL,
                    first_seen_at INTEGER NOT NULL,
                    last_seen_at INTEGER NOT NULL,
                    consecutive_samples INTEGER NOT NULL,
                    last_notified_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS gpu_process_actions (
                    token TEXT PRIMARY KEY,
                    telegram_user_id INTEGER NOT NULL,
                    incident_key TEXT NOT NULL,
                    host TEXT NOT NULL,
                    server_username TEXT NOT NULL,
                    identities TEXT NOT NULL,
                    status TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    term_requested_at INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            db.execute(
                """
                INSERT OR IGNORE INTO profiles (
                    telegram_user_id, telegram_username, full_name, recommender
                )
                SELECT telegram_user_id, telegram_username, full_name, recommender
                FROM users
                """
            )
            foreign_keys = db.execute("PRAGMA foreign_key_list(accesses)").fetchall()
            if any(row["table"] == "users" for row in foreign_keys):
                db.commit()
                db.execute("PRAGMA foreign_keys = OFF")
                db.executescript(
                    """
                    BEGIN;
                    CREATE TABLE accesses_v2 (
                        telegram_user_id INTEGER NOT NULL
                            REFERENCES profiles(telegram_user_id),
                        host TEXT NOT NULL,
                        secret BLOB NOT NULL,
                        created INTEGER NOT NULL,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (telegram_user_id, host)
                    );
                    INSERT INTO accesses_v2
                        (telegram_user_id, host, secret, created, updated_at)
                    SELECT telegram_user_id, host, secret, created, updated_at
                    FROM accesses;
                    DROP TABLE accesses;
                    ALTER TABLE accesses_v2 RENAME TO accesses;
                    COMMIT;
                    """
                )
                db.execute("PRAGMA foreign_keys = ON")
            db.execute(
                """
                UPDATE registry_users
                SET source_telegram_username =
                    lower(ltrim(source_telegram_username, '@'))
                """
            )
            duplicate_count = db.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT source_telegram_username
                    FROM registry_users
                    GROUP BY source_telegram_username COLLATE NOCASE
                    HAVING COUNT(*) > 1
                )
                """
            ).fetchone()[0]
            if duplicate_count:
                raise RuntimeError(
                    "registry ownership audit failed: duplicate source usernames"
                )
            db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    registry_users_source_username_unique
                ON registry_users(source_telegram_username COLLATE NOCASE)
                """
            )

    @staticmethod
    def _gpu_process_action_from_row(row: sqlite3.Row) -> GpuProcessAction:
        return GpuProcessAction(
            token=str(row["token"]),
            telegram_user_id=int(row["telegram_user_id"]),
            incident_key=str(row["incident_key"]),
            host=str(row["host"]),
            server_username=str(row["server_username"]),
            identities=tuple(
                ProcessIdentity(int(item["pid"]), int(item["start_ticks"]))
                for item in json.loads(str(row["identities"]))
            ),
            status=str(row["status"]),
            expires_at=int(row["expires_at"]),
            term_requested_at=(
                None
                if row["term_requested_at"] is None
                else int(row["term_requested_at"])
            ),
        )

    def save_gpu_process_action(self, action: GpuProcessAction) -> None:
        identities = json.dumps(
            [
                {"pid": item.pid, "start_ticks": item.start_ticks}
                for item in action.identities
            ]
        )
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO gpu_process_actions (
                    token, telegram_user_id, incident_key, host,
                    server_username, identities, status, expires_at,
                    term_requested_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action.token,
                    action.telegram_user_id,
                    action.incident_key,
                    action.host,
                    action.server_username,
                    identities,
                    action.status,
                    action.expires_at,
                    action.term_requested_at,
                ),
            )

    def gpu_process_action_for(
        self, token: str, telegram_user_id: int, *, now: int
    ) -> GpuProcessAction | None:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT * FROM gpu_process_actions
                WHERE token = ? AND telegram_user_id = ? AND expires_at >= ?
                """,
                (token, telegram_user_id, now),
            ).fetchone()
        return None if row is None else self._gpu_process_action_from_row(row)

    def transition_gpu_process_action(
        self,
        token: str,
        telegram_user_id: int,
        expected_status: str,
        new_status: str,
        *,
        now: int,
        expires_at: int | None = None,
        term_requested_at: int | None = None,
    ) -> GpuProcessAction | None:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT * FROM gpu_process_actions
                WHERE token = ? AND telegram_user_id = ?
                  AND status = ? AND expires_at >= ?
                """,
                (token, telegram_user_id, expected_status, now),
            ).fetchone()
            if row is None:
                return None
            updated = db.execute(
                """
                UPDATE gpu_process_actions
                SET status = ?, expires_at = COALESCE(?, expires_at),
                    term_requested_at = COALESCE(?, term_requested_at)
                WHERE token = ? AND telegram_user_id = ? AND status = ?
                """,
                (
                    new_status,
                    expires_at,
                    term_requested_at,
                    token,
                    telegram_user_id,
                    expected_status,
                ),
            )
            if updated.rowcount != 1:
                return None
        return self._gpu_process_action_from_row(row)

    def save_profile(self, profile: Profile) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO profiles (
                    telegram_user_id, telegram_username, full_name, recommender
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    telegram_username=excluded.telegram_username,
                    full_name=excluded.full_name,
                    recommender=excluded.recommender,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    profile.telegram_user_id,
                    profile.telegram_username,
                    profile.full_name,
                    profile.recommender,
                ),
            )

    def profile_for(self, telegram_user_id: int) -> Profile | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM profiles WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()
        if row is None:
            return None
        return Profile(
            telegram_user_id=telegram_user_id,
            telegram_username=row["telegram_username"],
            full_name=str(row["full_name"]),
            recommender=str(row["recommender"]),
        )

    def save_registry_match(
        self,
        telegram_user_id: int,
        telegram_username: str | None,
        match: RegistryMatch,
    ) -> None:
        if match.telegram_user_id != telegram_user_id:
            raise RegistryAlreadyClaimedError(
                "registry identity is not verified for this Telegram ID"
            )
        source_username = match.telegram_username.lstrip("@").casefold()
        try:
            with self._connect() as db:
                owner = db.execute(
                    """
                    SELECT telegram_user_id FROM registry_users
                    WHERE source_telegram_username = ? COLLATE NOCASE
                    """,
                    (source_username,),
                ).fetchone()
                current_link = db.execute(
                    """
                    SELECT source_telegram_username FROM registry_users
                    WHERE telegram_user_id = ?
                    """,
                    (telegram_user_id,),
                ).fetchone()
                if (
                    owner is not None
                    and int(owner["telegram_user_id"]) != telegram_user_id
                ):
                    raise RegistryAlreadyClaimedError(
                        "this registry identity is already linked"
                    )
                if (
                    current_link is not None
                    and str(current_link["source_telegram_username"]).casefold()
                    != source_username
                ):
                    raise RegistryAlreadyClaimedError(
                        "this Telegram ID is already linked to another identity"
                    )
                db.execute(
                    """
                INSERT INTO profiles (
                    telegram_user_id, telegram_username, full_name, recommender
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    telegram_username=excluded.telegram_username,
                    updated_at=CURRENT_TIMESTAMP
                """,
                    (
                        telegram_user_id,
                        telegram_username,
                        match.full_name,
                        match.recommender,
                    ),
                )
                db.execute(
                    """
                INSERT INTO registry_users (
                    telegram_user_id, source_telegram_username, server_username,
                    access_term, activity, source_row_count
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    source_telegram_username=excluded.source_telegram_username,
                    server_username=excluded.server_username,
                    access_term=excluded.access_term,
                    activity=excluded.activity,
                    source_row_count=excluded.source_row_count,
                    confirmed_at=CURRENT_TIMESTAMP
                """,
                    (
                        telegram_user_id,
                        source_username,
                        match.server_username,
                        match.access_term,
                        match.activity,
                        match.source_row_count,
                    ),
                )
                db.execute(
                    "DELETE FROM registry_services WHERE telegram_user_id = ?",
                    (telegram_user_id,),
                )
                db.executemany(
                    """
                    INSERT INTO registry_services (telegram_user_id, host)
                    VALUES (?, ?)
                    """,
                    [(telegram_user_id, host) for host in match.servers],
                )
        except sqlite3.IntegrityError as error:
            raise RegistryAlreadyClaimedError(
                "this registry identity is already linked"
            ) from error

    def registry_match_for(self, telegram_user_id: int) -> RegistryMatch | None:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT p.full_name, p.recommender,
                       r.source_telegram_username, r.server_username,
                       r.access_term, r.activity, r.source_row_count
                FROM registry_users r
                JOIN profiles p USING (telegram_user_id)
                WHERE r.telegram_user_id = ?
                """,
                (telegram_user_id,),
            ).fetchone()
            services = db.execute(
                """
                SELECT host FROM registry_services
                WHERE telegram_user_id = ?
                ORDER BY CASE host
                    WHEN 'shkodnik_opt' THEN 1
                    WHEN 'brain_lab' THEN 2
                    WHEN 'vv_h200' THEN 3
                    WHEN 'yandex' THEN 4
                    ELSE 5
                END
                """,
                (telegram_user_id,),
            ).fetchall()
        if row is None:
            return None
        return RegistryMatch(
            telegram_username=str(row["source_telegram_username"]),
            full_name=str(row["full_name"]),
            server_username=str(row["server_username"]),
            access_term=str(row["access_term"]),
            recommender=str(row["recommender"]),
            activity=str(row["activity"]),
            servers=tuple(str(item["host"]) for item in services),
            source_row_count=int(row["source_row_count"]),
            telegram_user_id=telegram_user_id,
        )

    def registry_services_for(self, telegram_user_id: int) -> tuple[str, ...]:
        match = self.registry_match_for(telegram_user_id)
        return () if match is None else match.servers

    def has_registered_service(self, telegram_user_id: int, host: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT 1 FROM registry_services
                WHERE telegram_user_id = ? AND host = ?
                """,
                (telegram_user_id, host),
            ).fetchone()
        return row is not None

    def save_application(self, application: Application) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO profiles (
                    telegram_user_id, telegram_username, full_name, recommender
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    telegram_username=excluded.telegram_username,
                    full_name=excluded.full_name,
                    recommender=excluded.recommender,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    application.telegram_user_id,
                    application.telegram_username,
                    application.full_name,
                    application.recommender,
                ),
            )
            db.execute(
                """
                INSERT INTO users (
                    telegram_user_id, telegram_username, full_name,
                    server_username, access_term, recommender, activity
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    telegram_username=excluded.telegram_username,
                    full_name=excluded.full_name,
                    server_username=excluded.server_username,
                    access_term=excluded.access_term,
                    recommender=excluded.recommender,
                    activity=excluded.activity,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    application.telegram_user_id,
                    application.telegram_username,
                    application.full_name,
                    application.server_username,
                    application.access_term,
                    application.recommender,
                    application.activity,
                ),
            )

    def server_username_for(self, telegram_user_id: int) -> str | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT server_username FROM users WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()
            if row is None:
                row = db.execute(
                    """
                    SELECT server_username FROM registry_users
                    WHERE telegram_user_id = ?
                    """,
                    (telegram_user_id,),
                ).fetchone()
        return None if row is None else str(row["server_username"])

    def telegram_user_id_for_server_username(self, server_username: str) -> int | None:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT DISTINCT telegram_user_id FROM (
                    SELECT telegram_user_id, server_username FROM users
                    UNION ALL
                    SELECT telegram_user_id, server_username FROM registry_users
                )
                WHERE server_username = ?
                LIMIT 2
                """,
                (server_username,),
            ).fetchall()
        if len(rows) != 1:
            return None
        return int(rows[0]["telegram_user_id"])

    def application_for(self, telegram_user_id: int) -> Application | None:
        with self._connect() as db:
            user = db.execute(
                "SELECT * FROM users WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()
            hosts = db.execute(
                "SELECT host FROM accesses WHERE telegram_user_id = ? ORDER BY host",
                (telegram_user_id,),
            ).fetchall()
        if user is None:
            return None
        return Application(
            telegram_user_id=telegram_user_id,
            telegram_username=user["telegram_username"],
            full_name=str(user["full_name"]),
            server_username=str(user["server_username"]),
            access_term=str(user["access_term"]),
            recommender=str(user["recommender"]),
            activity=str(user["activity"]),
            servers=tuple(str(row["host"]) for row in hosts),
        )

    def save_access(self, telegram_user_id: int, access: ProvisionedAccess) -> None:
        if not access.created and self.has_access(telegram_user_id, access.host):
            return
        payload = json.dumps(
            {
                "host": access.host,
                "username": access.username,
                "password": access.password,
                "connection": access.connection,
                "group_url": access.group_url,
            },
            ensure_ascii=False,
        ).encode()
        nonce = os.urandom(12)
        secret = nonce + self.cipher.encrypt(
            nonce, payload, str(telegram_user_id).encode()
        )
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO accesses (telegram_user_id, host, secret, created)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_user_id, host) DO UPDATE SET
                    secret=excluded.secret,
                    created=excluded.created,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (telegram_user_id, access.host, secret, int(access.created)),
            )

    def record_event(
        self,
        telegram_user_id: int,
        event: str,
        host: str | None = None,
        detail: str | None = None,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO audit_events (telegram_user_id, event, host, detail)
                VALUES (?, ?, ?, ?)
                """,
                (telegram_user_id, event, host, detail),
            )

    def save_pending_request(
        self, request_id: str, application: Application
    ) -> None:
        payload = json.dumps(
            {
                "telegram_user_id": application.telegram_user_id,
                "telegram_username": application.telegram_username,
                "full_name": application.full_name,
                "server_username": application.server_username,
                "access_term": application.access_term,
                "recommender": application.recommender,
                "activity": application.activity,
                "servers": list(application.servers),
            },
            ensure_ascii=False,
        )
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO pending_requests (
                    request_id, telegram_user_id, payload
                ) VALUES (?, ?, ?)
                """,
                (request_id, application.telegram_user_id, payload),
            )

    def claim_pending_request(self, request_id: str) -> Application | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT payload FROM pending_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                return None
            updated = db.execute(
                """
                UPDATE pending_requests SET status = 'processing'
                WHERE request_id = ? AND status = 'pending'
                """,
                (request_id,),
            )
            if updated.rowcount != 1:
                return None
            data = json.loads(str(row["payload"]))
        return Application(
            telegram_user_id=int(data["telegram_user_id"]),
            telegram_username=data["telegram_username"],
            full_name=str(data["full_name"]),
            server_username=str(data["server_username"]),
            access_term=str(data["access_term"]),
            recommender=str(data["recommender"]),
            activity=str(data["activity"]),
            servers=tuple(str(host) for host in data["servers"]),
        )

    def resolve_pending_request(self, request_id: str, status: str) -> None:
        if status not in {"approved", "rejected", "failed"}:
            raise ValueError("invalid pending request status")
        with self._connect() as db:
            db.execute(
                """
                UPDATE pending_requests
                SET status = ?, resolved_at = CURRENT_TIMESTAMP
                WHERE request_id = ?
                """,
                (status, request_id),
            )

    def has_access(self, telegram_user_id: int, host: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT 1 FROM accesses WHERE telegram_user_id = ? AND host = ?",
                (telegram_user_id, host),
            ).fetchone()
        return row is not None

    def credentials_for(self, telegram_user_id: int) -> list[ProvisionedAccess]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT host, secret, created FROM accesses
                WHERE telegram_user_id = ? ORDER BY host
                """,
                (telegram_user_id,),
            ).fetchall()
        result = []
        for row in rows:
            secret = bytes(row["secret"])
            payload = json.loads(
                self.cipher.decrypt(
                    secret[:12], secret[12:], str(telegram_user_id).encode()
                )
            )
            result.append(ProvisionedAccess(created=bool(row["created"]), **payload))
        return result

    def update_gpu_idle_observations(
        self,
        candidates: tuple[GpuIdleCandidate, ...],
        *,
        reachable_hosts: tuple[str, ...],
        now: int,
        minimum_duration_seconds: int,
        minimum_samples: int,
        maximum_sample_gap_seconds: int,
        notification_cooldown_seconds: int,
    ) -> list[GpuIdleIncident]:
        candidate_keys = {
            f"{item.host}:{item.gpu_uuid}:{item.server_username}": item
            for item in candidates
        }
        reachable = set(reachable_hosts)
        with self._connect() as db:
            active_rows = db.execute(
                "SELECT incident_key, host FROM gpu_idle_observations"
            ).fetchall()
            for row in active_rows:
                key = str(row["incident_key"])
                if str(row["host"]) in reachable and key not in candidate_keys:
                    db.execute(
                        """
                        UPDATE gpu_idle_observations
                        SET consecutive_samples = 0
                        WHERE incident_key = ?
                        """,
                        (key,),
                    )

            for key, candidate in candidate_keys.items():
                row = db.execute(
                    """
                    SELECT first_seen_at, last_seen_at, consecutive_samples
                    FROM gpu_idle_observations WHERE incident_key = ?
                    """,
                    (key,),
                ).fetchone()
                continues = (
                    row is not None
                    and int(row["consecutive_samples"]) > 0
                    and 0 <= now - int(row["last_seen_at"])
                    <= maximum_sample_gap_seconds
                )
                first_seen_at = int(row["first_seen_at"]) if continues else now
                consecutive_samples = (
                    int(row["consecutive_samples"]) + 1 if continues else 1
                )
                db.execute(
                    """
                    INSERT INTO gpu_idle_observations (
                        incident_key, host, gpu_uuid, gpu_index,
                        server_username, used_memory_mb, pids,
                        first_seen_at, last_seen_at, consecutive_samples
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(incident_key) DO UPDATE SET
                        gpu_index=excluded.gpu_index,
                        used_memory_mb=excluded.used_memory_mb,
                        pids=excluded.pids,
                        first_seen_at=excluded.first_seen_at,
                        last_seen_at=excluded.last_seen_at,
                        consecutive_samples=excluded.consecutive_samples
                    """,
                    (
                        key,
                        candidate.host,
                        candidate.gpu_uuid,
                        candidate.gpu_index,
                        candidate.server_username,
                        candidate.used_memory_mb,
                        json.dumps(candidate.pids),
                        first_seen_at,
                        now,
                        consecutive_samples,
                    ),
                )

            result = []
            for key in candidate_keys:
                row = db.execute(
                    "SELECT * FROM gpu_idle_observations WHERE incident_key = ?",
                    (key,),
                ).fetchone()
                if row is None:
                    continue
                last_notified = row["last_notified_at"]
                if (
                    int(row["consecutive_samples"]) < minimum_samples
                    or now - int(row["first_seen_at"]) < minimum_duration_seconds
                    or (
                        last_notified is not None
                        and now - int(last_notified) < notification_cooldown_seconds
                    )
                ):
                    continue
                result.append(
                    GpuIdleIncident(
                        key=key,
                        host=str(row["host"]),
                        gpu_index=int(row["gpu_index"]),
                        server_username=str(row["server_username"]),
                        used_memory_mb=int(row["used_memory_mb"]),
                        pids=tuple(int(pid) for pid in json.loads(str(row["pids"]))),
                        first_seen_at=int(row["first_seen_at"]),
                        last_seen_at=int(row["last_seen_at"]),
                    )
                )
        return sorted(
            result,
            key=lambda item: (item.server_username, item.host, item.gpu_index),
        )

    def mark_gpu_idle_notified(self, keys: tuple[str, ...], *, now: int) -> None:
        if not keys:
            return
        with self._connect() as db:
            db.executemany(
                """
                UPDATE gpu_idle_observations SET last_notified_at = ?
                WHERE incident_key = ?
                """,
                [(now, key) for key in keys],
            )
