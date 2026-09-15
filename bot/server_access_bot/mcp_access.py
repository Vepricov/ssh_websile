"""Owner-approved MCP access, separate from Unix server provisioning."""

import json
import os
import secrets
import subprocess
import threading

from .provisioning import SSH_OPTIONS
from .storage import Storage


class McpBridge:
    def _call(self, action: str, user_id: int, token: str = "") -> dict:
        result = subprocess.run(
            ["ssh", *SSH_OPTIONS, "brain_lab", "/home/shkodnik1917/.local/bin/brainlab-mcp-access"],
            input=json.dumps({"action": action, "telegram_user_id": user_id, "token": token}),
            text=True,
            capture_output=True,
            timeout=45,
            check=False,
        )
        # Never include child output in an exception: it may contain credentials.
        if result.returncode:
            raise RuntimeError("MCP access service unavailable")
        data = json.loads(result.stdout)
        if (data.get("role") != "member"
                or data.get("email") != f"telegram-{user_id}@users.brainlab-ai.com"):
            raise RuntimeError("Unexpected MCP account role")
        return data

    def issue(self, user_id: int, token: str) -> dict:
        return self._call("issue", user_id, token)

    def revoke(self, user_id: int) -> dict:
        return self._call("revoke", user_id)


class McpAccessService:
    def __init__(self, storage: Storage, bridge: McpBridge, admins: frozenset[int]):
        self.storage = storage
        self.bridge = bridge
        self.admins = admins
        self._lock = threading.Lock()
        with storage._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS mcp_requests (
                    request_id TEXT PRIMARY KEY,
                    telegram_user_id INTEGER NOT NULL UNIQUE,
                    full_name TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    secret BLOB NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    approved_by INTEGER,
                    account TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)

    def _admin(self, user_id: int) -> None:
        if user_id not in self.admins:
            raise PermissionError("Only the access owner can approve requests")

    def get(self, user_id: int) -> dict | None:
        with self.storage._connect() as db:
            row = db.execute(
                "SELECT * FROM mcp_requests WHERE telegram_user_id=?", (user_id,)
            ).fetchone()
        if row is None:
            return None
        # Public status never includes the encrypted or plaintext key.
        return {key: row[key] for key in row.keys() if key != "secret"}

    def pending(self, admin_id: int) -> list[dict]:
        self._admin(admin_id)
        with self.storage._connect() as db:
            rows = db.execute(
                "SELECT telegram_user_id FROM mcp_requests "
                "WHERE status IN ('pending','approved','granted') ORDER BY created_at"
            ).fetchall()
        return [self.get(row[0]) for row in rows]

    def request(self, user_id: int, full_name: str, purpose: str = "") -> dict:
        if not self.admins:
            raise RuntimeError("MCP approvals are not configured")
        if user_id <= 0 or not full_name.strip() or (purpose.strip() and not 3 <= len(purpose.strip()) <= 1000):
            raise ValueError("Некорректные данные заявки.")
        with self._lock:
            previous = self.get(user_id)
            if previous and previous["status"] not in {"rejected", "revoked"}:
                return {**previous, "created": False}
            request_id = secrets.token_hex(12)
            token = secrets.token_hex(32).encode()
            nonce = os.urandom(12)
            secret = nonce + self.storage.cipher.encrypt(nonce, token, str(user_id).encode())
            with self.storage._connect() as db:
                db.execute("""
                    INSERT INTO mcp_requests
                        (request_id, telegram_user_id, full_name, purpose, secret)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(telegram_user_id) DO UPDATE SET
                        request_id=excluded.request_id, full_name=excluded.full_name,
                        purpose=excluded.purpose, secret=excluded.secret, status='pending',
                        approved_by=NULL, account=NULL, updated_at=CURRENT_TIMESTAMP
                """, (request_id, user_id, full_name.strip()[:200], purpose.strip(), secret))
            self.storage.record_event(user_id, "mcp.requested", detail=request_id)
            return {**self.get(user_id), "created": True}

    def approve(self, admin_id: int, request_id: str) -> dict:
        self._admin(admin_id)
        with self._lock:
            with self.storage._connect() as db:
                row = db.execute(
                    "SELECT * FROM mcp_requests WHERE request_id=?", (request_id,)
                ).fetchone()
                if row is None or row["status"] in {"rejected", "revoked"}:
                    raise ValueError("Заявка уже закрыта.")
                db.execute(
                    "UPDATE mcp_requests SET approved_by=?, status='approved', "
                    "updated_at=CURRENT_TIMESTAMP WHERE request_id=? AND status='pending'",
                    (admin_id, request_id),
                )
            user_id = row["telegram_user_id"]
            secret = row["secret"]
            token = self.storage.cipher.decrypt(secret[:12], secret[12:], str(user_id).encode()).decode()
            if row["status"] in {"granted", "delivered"}:
                account = json.loads(row["account"])
            else:
                # Same token on every retry, including a crash after the remote commit.
                account = self.bridge.issue(user_id, token)
                with self.storage._connect() as db:
                    db.execute(
                        "UPDATE mcp_requests SET account=?, status='granted', "
                        "updated_at=CURRENT_TIMESTAMP WHERE request_id=?",
                        (json.dumps(account), request_id),
                    )
                self.storage.record_event(user_id, "mcp.granted", detail=f"{request_id}; approver={admin_id}")
            return {**account, "telegram_user_id": user_id, "token": token, "request_id": request_id}

    def delivered(self, request_id: str) -> None:
        with self.storage._connect() as db:
            db.execute(
                "UPDATE mcp_requests SET status='delivered', updated_at=CURRENT_TIMESTAMP "
                "WHERE request_id=? AND status='granted'", (request_id,)
            )

    def reject(self, admin_id: int, request_id: str) -> int:
        self._admin(admin_id)
        with self._lock, self.storage._connect() as db:
            row = db.execute(
                "UPDATE mcp_requests SET status='rejected', updated_at=CURRENT_TIMESTAMP "
                "WHERE request_id=? AND status='pending' RETURNING telegram_user_id",
                (request_id,),
            ).fetchone()
        if row is None:
            raise ValueError("Заявка уже обработана. Для выданного ключа нужен отзыв.")
        self.storage.record_event(row[0], "mcp.rejected", detail=f"{request_id}; approver={admin_id}")
        return row[0]

    def revoke(self, actor_id: int, user_id: int) -> None:
        if actor_id != user_id:
            self._admin(actor_id)
        with self._lock:
            row = self.get(user_id)
            if not row or row["status"] not in {"approved", "granted", "delivered"}:
                raise ValueError("Нет выданного ключа.")
            self.bridge.revoke(user_id)
            with self.storage._connect() as db:
                db.execute(
                    "UPDATE mcp_requests SET status='revoked', secret=X'', "
                    "updated_at=CURRENT_TIMESTAMP WHERE telegram_user_id=?", (user_id,)
                )
            self.storage.record_event(user_id, "mcp.revoked", detail=f"actor={actor_id}")
