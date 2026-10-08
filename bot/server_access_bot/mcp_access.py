"""Owner-approved MCP access, separate from Unix server provisioning."""

import json
import os
import secrets
import subprocess
import threading

from .provisioning import SSH_OPTIONS
from .storage import Storage
from .mcp_profile import ROLES


class McpBridge:
    def _call(self, action: str, user_id: int, token: str = "", role: str = "member",
              username: str = "", full_name: str = "") -> dict:
        # Имя и полное имя нужны, чтобы учётка на сайте называлась по-человечески, а не
        # tg123456789. Оба необязательны: без них сервер соберёт имя сам.
        payload = {"action": action, "telegram_user_id": user_id, "token": token, "role": role}
        if username:
            payload["username"] = username
        if full_name:
            payload["full_name"] = full_name
        result = subprocess.run(
            ["ssh", *SSH_OPTIONS, "brain_lab", "/home/shkodnik1917/.local/bin/brainlab-mcp-access"],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=45,
            check=False,
        )
        # Полезную нагрузку наружу не отдаём, а причину — отдаём. Дочерний скрипт по
        # своему контракту печатает в stderr только имя исключения и его текст, без
        # ключей и без запроса. Прежде здесь терялось и оно, и в журнале оставался
        # голый RuntimeError: 04-10-2026 на этом встали две заявки, и причину пришлось
        # искать запуском скрипта руками.
        if result.returncode:
            lines = (result.stderr or "").strip().splitlines()
            detail = lines[-1][:200] if lines else ""
            raise RuntimeError(
                f"MCP access service unavailable: {detail}" if detail
                else "MCP access service unavailable")
        data = json.loads(result.stdout)
        if (data.get("role") not in ROLES or data.get("telegram_user_id") != user_id
                or not data.get("email") or not data.get("user_id")):
            raise RuntimeError("Unexpected MCP account role")
        return data

    def issue(self, user_id: int, token: str, role: str = "member",
              username: str = "", full_name: str = "") -> dict:
        return self._call("issue", user_id, token, role, username, full_name)

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
            columns = {row[1] for row in db.execute("PRAGMA table_info(mcp_requests)")}
            for name, default in (("username", ""), ("telegram_username", ""),
                                  ("recommender", ""), ("requested_role", "member"),
                                  ("approved_role", "")):
                if name not in columns:
                    db.execute(f"ALTER TABLE mcp_requests ADD COLUMN {name} TEXT NOT NULL DEFAULT '{default}'")
            db.execute("UPDATE mcp_requests SET approved_role='member' "
                       "WHERE approved_role='' AND status IN ('approved','granted','delivered')")

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

    def request(self, user_id: int, full_name: str, purpose: str = "", *,
                username: str = "", telegram_username: str = "", recommender: str = "",
                requested_role: str = "member") -> dict:
        if not self.admins:
            raise RuntimeError("MCP approvals are not configured")
        if user_id <= 0 or not full_name.strip() or len(purpose.strip()) > 1000:
            raise ValueError("Некорректные данные заявки.")
        if requested_role not in ROLES:
            raise ValueError("Неизвестная роль.")
        with self._lock:
            previous = self.get(user_id)
            if previous and previous["status"] not in {"rejected", "revoked"}:
                if previous["status"] in {"granted", "delivered"} and username:
                    current_role = json.loads(previous["account"])["role"]
                    if list(ROLES).index(requested_role) > list(ROLES).index(current_role):
                        request_id = secrets.token_hex(12)
                        with self.storage._connect() as db:
                            db.execute(
                                "UPDATE mcp_requests SET request_id=?, full_name=?, username=?, "
                                "telegram_username=?, recommender=?, purpose=?, requested_role=?, "
                                "approved_role='', status='pending', updated_at=CURRENT_TIMESTAMP "
                                "WHERE telegram_user_id=?",
                                (request_id, full_name, username, telegram_username or "", recommender,
                                 purpose, requested_role, user_id),
                            )
                        self.storage.record_event(user_id, "mcp.role_requested", detail=request_id)
                        return {**self.get(user_id), "created": True}
                    with self.storage._connect() as db:
                        db.execute("UPDATE mcp_requests SET full_name=?, username=?, telegram_username=?, "
                                   "recommender=?, updated_at=CURRENT_TIMESTAMP WHERE telegram_user_id=?",
                                   (full_name, username, telegram_username or "", recommender, user_id))
                    previous = self.get(user_id)
                return {**previous, "created": False}
            request_id = secrets.token_hex(12)
            token = secrets.token_hex(32).encode()
            nonce = os.urandom(12)
            secret = nonce + self.storage.cipher.encrypt(nonce, token, str(user_id).encode())
            with self.storage._connect() as db:
                db.execute("""
                    INSERT INTO mcp_requests
                        (request_id, telegram_user_id, full_name, purpose, secret,
                         username, telegram_username, recommender, requested_role)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(telegram_user_id) DO UPDATE SET
                        request_id=excluded.request_id, full_name=excluded.full_name,
                        purpose=excluded.purpose, secret=excluded.secret, status='pending',
                        username=excluded.username, telegram_username=excluded.telegram_username,
                        recommender=excluded.recommender, requested_role=excluded.requested_role,
                        approved_role='', approved_by=NULL, account=NULL, updated_at=CURRENT_TIMESTAMP
                """, (request_id, user_id, full_name.strip()[:200], purpose.strip(), secret,
                      username, telegram_username or "", recommender, requested_role))
            self.storage.record_event(user_id, "mcp.requested", detail=request_id)
            return {**self.get(user_id), "created": True}

    def approve(self, admin_id: int, request_id: str, role: str | None = None) -> dict:
        self._admin(admin_id)
        with self._lock:
            with self.storage._connect() as db:
                row = db.execute(
                    "SELECT * FROM mcp_requests WHERE request_id=?", (request_id,)
                ).fetchone()
                if row is None or row["status"] in {"rejected", "revoked"}:
                    raise ValueError("Заявка уже закрыта.")
                chosen_role = role or row["approved_role"] or row["requested_role"]
                if chosen_role not in ROLES:
                    raise ValueError("Неизвестная роль.")
                if row["approved_role"] and chosen_role != row["approved_role"]:
                    raise ValueError("Роль этой заявки уже одобрена. Повтор не меняет права.")
                db.execute(
                    "UPDATE mcp_requests SET approved_by=?, approved_role=?, status='approved', "
                    "updated_at=CURRENT_TIMESTAMP WHERE request_id=? AND status='pending'",
                    (admin_id, chosen_role, request_id),
                )
            user_id = row["telegram_user_id"]
            secret = row["secret"]
            token = self.storage.cipher.decrypt(secret[:12], secret[12:], str(user_id).encode()).decode()
            if row["status"] in {"granted", "delivered"}:
                account = json.loads(row["account"])
            else:
                # Same token on every retry, including a crash after the remote commit.
                account = self.bridge.issue(
                    user_id, token, chosen_role,
                    username=str(row["username"] or ""), full_name=str(row["full_name"] or ""))
                with self.storage._connect() as db:
                    db.execute(
                        "UPDATE mcp_requests SET account=?, status='granted', "
                        "updated_at=CURRENT_TIMESTAMP WHERE request_id=?",
                        (json.dumps(account), request_id),
                    )
                self.storage.record_event(user_id, "mcp.granted", detail=f"{request_id}; approver={admin_id}")
            return {**account, "telegram_user_id": user_id, "token": token, "request_id": request_id,
                    "full_name": row["full_name"], "username": row["username"],
                    "telegram_username": row["telegram_username"]}

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
                "UPDATE mcp_requests SET status=CASE WHEN account IS NULL THEN 'rejected' ELSE 'delivered' END, "
                "approved_role=CASE WHEN account IS NULL THEN '' ELSE json_extract(account, '$.role') END, "
                "updated_at=CURRENT_TIMESTAMP "
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
            if not row or (row["status"] not in {"approved", "granted", "delivered"}
                           and not (row["status"] == "pending" and row["account"])):
                raise ValueError("Нет выданного ключа.")
            self.bridge.revoke(user_id)
            with self.storage._connect() as db:
                db.execute(
                    "UPDATE mcp_requests SET status='revoked', secret=X'', "
                    "updated_at=CURRENT_TIMESTAMP WHERE telegram_user_id=?", (user_id,)
                )
            self.storage.record_event(user_id, "mcp.revoked", detail=f"actor={actor_id}")
