#!/usr/bin/env python3
"""Owner-approved roles for a stable Telegram identity, with pinned existing users.

The caller sends JSON on stdin. Tokens never occur in argv or stdout. Deployment
loads the existing MCP service environment, including its local DB connection.
"""

import json
import os
import re
import sys
from pathlib import Path


def grant(service, actor, payload: dict, bindings: dict | None = None) -> dict:
    from lab_knowledge.errors import AuthenticationError, AuthorizationError, ValidationError
    from lab_knowledge.models import Role

    if set(payload) not in ({"action", "telegram_user_id", "token"},
                            {"action", "telegram_user_id", "token", "role"}):
        raise ValidationError("Unexpected request fields")
    user_id = payload["telegram_user_id"]
    if type(user_id) is not int or not 0 < user_id < 2**52:
        raise ValidationError("Invalid Telegram ID")
    action = payload["action"]
    if action not in {"issue", "revoke"}:
        raise ValidationError("Unsupported action")
    token = payload["token"]
    if not isinstance(token, str) or (action == "issue" and not re.fullmatch(r"[0-9a-f]{64}", token)):
        raise ValidationError("Invalid token")
    if actor.role is not Role.LEAD:
        raise AuthorizationError("Only the access owner can approve roles")
    try:
        requested_role = Role(payload.get("role", "member"))
    except (ValueError, TypeError) as error:
        raise ValidationError("Invalid role") from error
    email = f"telegram-{user_id}@users.brainlab-ai.com"
    label = f"telegram:{user_id}"
    repo = service.repository
    with repo.idempotency_scope(actor.id, "telegram_access", str(user_id)):
        pinned_id = (bindings or {}).get(str(user_id))
        if pinned_id:
            owner = next((u for u in repo.list_users() if str(u.id) == pinned_id), None)
            if owner is None:
                raise AuthorizationError("Pinned account not found")
            email = owner.email
        else:
            owner = repo.user_by_email(email)
        if owner is not None and not owner.active:
            raise AuthorizationError("Account is disabled")
        if action == "revoke":
            count = service.revoke_agent_token(actor, label, email=email) if owner else 0
            return {"email": email, "role": owner.role.value if owner else "member",
                    "user_id": str(owner.id) if owner else "", "telegram_user_id": user_id,
                    "revoked": count}
        owner = owner or service.invite_member(actor, email)
        with repo.idempotency_scope(owner.id, "account_keys", "access"):
            owner = repo.user_by_email(owner.email)
            if owner is None or not owner.active:
                raise AuthorizationError("Account is disabled")
            rank = {Role.MEMBER: 0, Role.MANAGER: 1, Role.LEAD: 2}
            if rank[requested_role] > rank[owner.role]:
                keys = repo.tokens_of_user(owner.id)
                if len(keys) > 1 or any(t["label"] != label for t in keys):
                    raise AuthorizationError("Review existing keys before promoting this account")
            try:
                existing, existing_label = service.authenticate_caller(token)
            except AuthenticationError:
                existing = None
            if existing is not None:
                if existing.id != owner.id or existing_label != label:
                    raise AuthorizationError("Token belongs to another identity")
            else:
                # Never silently rotate a key that was already issued. A missing local
                # database must be repaired or explicitly revoked by the owner first.
                if any(t["label"] == label and not t.get("revoked_at") for t in repo.tokens_of_user(owner.id)):
                    raise AuthorizationError("Existing access needs recovery or revocation")
                service.issue_agent_token(actor, label, token, email=email)
            if rank[requested_role] > rank[owner.role]:
                owner = service.set_user_role(actor, email, requested_role)
            authenticated = service.authenticate(token)
            if authenticated.id != owner.id or authenticated.role is not owner.role:
                raise AuthorizationError("Access verification failed")
            return {"email": email, "role": owner.role.value, "user_id": str(owner.id),
                    "telegram_user_id": user_id}


def main() -> int:
    root = Path(os.environ.get("LAB_MCP_ROOT", "/home/shkodnik1917/brainlab-stack/lab-knowledge"))
    sys.path[:0] = [str(root / "scripts"), str(root / "src")]
    from grant_mcp_access import connect

    try:
        raw = sys.stdin.read(4097)
        if len(raw) > 4096:
            raise ValueError("Request too large")
        service, actor = connect()
        identity_file = root.parent / "secrets" / "telegram-mcp-identities.json"
        bindings = json.loads(identity_file.read_text()) if identity_file.exists() else {}
        result = grant(service, actor, json.loads(raw), bindings)
    except Exception as error:
        # Do not echo payloads, SQL arguments or connection strings.
        print(type(error).__name__, file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
