#!/usr/bin/env python3
"""Narrow SSH bridge: a numeric Telegram identity can receive only member access.

The caller sends JSON on stdin. Tokens never occur in argv or stdout. Deployment
loads the existing MCP service environment, including its local DB connection.
"""

import json
import os
import re
import sys
from pathlib import Path


def grant(service, actor, payload: dict) -> dict:
    from lab_knowledge.errors import AuthenticationError, AuthorizationError, ValidationError
    from lab_knowledge.models import Role

    if set(payload) != {"action", "telegram_user_id", "token"}:
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
    email = f"telegram-{user_id}@users.brainlab-ai.com"
    label = f"telegram:{user_id}"
    repo = service.repository
    with repo.idempotency_scope(actor.id, "telegram_access", str(user_id)):
        owner = repo.user_by_email(email)
        if owner is not None and (owner.role is not Role.MEMBER or not owner.active):
            raise AuthorizationError("Account needs manual review")
        if action == "revoke":
            count = service.revoke_agent_token(actor, label, email=email) if owner else 0
            return {"email": email, "role": "member", "revoked": count}
        owner = service.invite_member(actor, email)
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
        authenticated = service.authenticate(token)
        if authenticated.id != owner.id or authenticated.role is not Role.MEMBER:
            raise AuthorizationError("Access verification failed")
        return {"email": email, "role": "member", "user_id": str(owner.id)}


def main() -> int:
    root = Path(os.environ.get("LAB_MCP_ROOT", "/home/shkodnik1917/brainlab-stack/lab-knowledge"))
    sys.path[:0] = [str(root / "scripts"), str(root / "src")]
    from grant_mcp_access import connect

    try:
        raw = sys.stdin.read(4097)
        if len(raw) > 4096:
            raise ValueError("Request too large")
        service, actor = connect()
        result = grant(service, actor, json.loads(raw))
    except Exception as error:
        # Do not echo payloads, SQL arguments or connection strings.
        print(type(error).__name__, file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
