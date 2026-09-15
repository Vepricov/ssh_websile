"""Rebind one verified owner's existing Telegram key; never change account roles.

Operator-only repair: the expected token hash must come from the bot's stored
credential. This function is deliberately not exposed through an MCP tool.
"""

import re
from uuid import UUID


def repair_owner(service, actor, telegram_id: int, source_id: str, target_id: str,
                 token_hash: str) -> dict:
    from lab_knowledge.errors import AuthorizationError
    from lab_knowledge.models import AuditEvent, Role

    if (actor.role is not Role.LEAD or str(actor.id) != target_id
            or type(telegram_id) is not int or telegram_id <= 0
            or not re.fullmatch(r"[0-9a-f]{64}", token_hash)):
        raise AuthorizationError("Owner identity must be verified")
    source, target = UUID(source_id), UUID(target_id)
    if source == target:
        raise AuthorizationError("Distinct source and target required")
    label = f"telegram:{telegram_id}"
    repo = service.repository
    with repo.idempotency_scope(actor.id, "telegram_access", str(telegram_id)):
        with repo._connection() as db:
            users = db.execute("SELECT id,email,role,active FROM users WHERE id IN (%s,%s) FOR UPDATE",
                               (source, target)).fetchall()
            by_id = {row["id"]: row for row in users}
            if (source not in by_id or target not in by_id
                    or by_id[source]["email"] != f"telegram-{telegram_id}@users.brainlab-ai.com"
                    or by_id[source]["role"] != "member" or not by_id[source]["active"]
                    or by_id[target]["role"] != "lead" or not by_id[target]["active"]):
                raise AuthorizationError("Account state changed; review required")
            rows = db.execute("SELECT user_id,token_hash FROM access_tokens "
                              "WHERE user_id IN (%s,%s) AND label=%s AND revoked_at IS NULL FOR UPDATE",
                              (source, target, label)).fetchall()
            if len(rows) != 1 or rows[0]["token_hash"] != token_hash:
                raise AuthorizationError("Exact existing Telegram key required")
            changed = rows[0]["user_id"] == source
            if changed:
                result = db.execute("UPDATE access_tokens SET user_id=%s WHERE user_id=%s "
                                    "AND token_hash=%s AND label=%s AND revoked_at IS NULL",
                                    (target, source, token_hash, label))
                if result.rowcount != 1:
                    raise AuthorizationError("Key changed during repair")
                repo.audit(AuditEvent(actor.id, "telegram_owner_rebound", "user", target, None,
                                      {"source_user_id": source_id, "target_user_id": target_id,
                                       "telegram_user_id": telegram_id, "label": label}))
            return {"email": by_id[target]["email"], "user_id": target_id, "role": "lead",
                    "telegram_user_id": telegram_id, "changed": changed}
