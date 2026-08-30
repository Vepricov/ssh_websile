import json
import os
import sqlite3
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .domain import Application, ProvisionedAccess


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
                CREATE TABLE IF NOT EXISTS accesses (
                    telegram_user_id INTEGER NOT NULL REFERENCES users(telegram_user_id),
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
                """
            )

    def save_application(self, application: Application) -> None:
        with self._connect() as db:
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
        return None if row is None else str(row["server_username"])

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
