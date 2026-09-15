"""Run in the MCP runtime with a disposable TEST_DATABASE_URL, never production."""

import asyncio
import json
import os
import secrets
from urllib.parse import urlparse

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from lab_knowledge.errors import AuthenticationError, AuthorizationError
from lab_knowledge.mcp_server import _request_user, create_mcp
from lab_knowledge.models import JournalEntry, ProjectRole, Role
from lab_knowledge.postgres import PostgresRepository
from lab_knowledge.service import LabKnowledgeService
from telegram_mcp_access import grant


@pytest.fixture
def scenario():
    url = os.environ["TEST_DATABASE_URL"]
    assert "_test_" in urlparse(url).path
    service = LabKnowledgeService(PostgresRepository(url))
    suffix = secrets.token_hex(5)
    lead = service.bootstrap_user(f"owner-{suffix}@example.test", Role.LEAD, secrets.token_hex(32))
    user_id = secrets.randbelow(10**12) + 1
    token = secrets.token_hex(32)
    payload = {"action": "issue", "telegram_user_id": user_id, "token": token}
    return service, lead, payload


def test_issue_replay_revoke_and_reissue_in_postgres(scenario):
    service, lead, payload = scenario
    first = grant(service, lead, payload)
    second = grant(service, lead, payload)
    assert first == second
    member = service.authenticate(payload["token"])
    assert member.role is Role.MEMBER
    assert len(service.repository.tokens_of_user(member.id)) == 1
    with pytest.raises(AuthorizationError):
        grant(service, lead, {**payload, "token": secrets.token_hex(32)})
    result = grant(service, lead, {**payload, "action": "revoke", "token": ""})
    assert result["revoked"] == 1
    with pytest.raises(AuthenticationError):
        service.authenticate(payload["token"])
    assert grant(service, lead, {**payload, "action": "revoke", "token": ""})["revoked"] == 0
    new = {**payload, "token": secrets.token_hex(32)}
    assert grant(service, lead, new)["user_id"] == first["user_id"]


def test_new_member_uses_published_mcp_tools_and_project_permissions(scenario):
    service, lead, payload = scenario
    grant(service, lead, payload)
    member = service.authenticate(payload["token"])
    mcp = create_mcp(service)

    def call(tool_name, **arguments):
        marker = _request_user.set(member)
        try:
            result = asyncio.run(mcp.call_tool(tool_name, arguments))
            if isinstance(result, tuple):
                return result[1]
            return json.loads(result[0].text)
        finally:
            _request_user.reset(marker)

    own = call("create_project", name="Participant onboarding test", slug=f"onboard-{payload['telegram_user_id']}",
               idempotency_key="create-own")
    project_id = own["id"]
    hypothesis = call("create_hypothesis", project_id=project_id,
                      statement="A fixed seed reproduces the same initialization.",
                      falsification_criteria="Initial weights differ with the same seed and environment.",
                      idempotency_key="hypothesis-own")
    call("update_hypothesis", hypothesis_id=hypothesis["id"],
         statement="A fixed seed and environment reproduce the initialization.", idempotency_key="edit-own")
    call("delete_record", entity_type="hypothesis", entity_id=hypothesis["id"])
    with pytest.raises(ToolError, match="project is not empty"):
        call("delete_project", project_id=project_id)
    call("delete_project", project_id=project_id, force=True)

    other = service.create_project(lead, "A different participant's project", slug=f"other-{payload['telegram_user_id']}")
    entry = service.record_journal(lead, JournalEntry("Test observation", lead.id, project_id=other.id))
    assert call("get_project_by_slug", slug=other.slug)
    with pytest.raises(ToolError):
        call("record_journal", body="Uninvited write", project_id=str(other.id), idempotency_key="uninvited")
    with pytest.raises(ToolError):
        call("delete_record", entity_type="journal", entity_id=str(entry.id))
    with pytest.raises(ToolError):
        call("delete_project", project_id=str(other.id), force=True)
    service.grant_project(lead, member.id, other.id, project_role=ProjectRole.VIEWER)
    with pytest.raises(ToolError):
        call("record_journal", body="Viewer write", project_id=str(other.id), idempotency_key="viewer")
    service.grant_project(lead, member.id, other.id, project_role=ProjectRole.CONTRIBUTOR)
    written = call("record_journal", body="Collaborator observation", project_id=str(other.id), idempotency_key="collaborator")
    assert written["id"]
    call("delete_record", entity_type="journal", entity_id=str(entry.id))
    with pytest.raises(ToolError):
        call("delete_project", project_id=str(other.id), force=True)


def test_bridge_refuses_privilege_escalation_and_cross_identity_token(scenario):
    service, lead, payload = scenario
    grant(service, lead, payload)
    member = service.authenticate(payload["token"])
    with pytest.raises(AuthorizationError):
        grant(service, member, {**payload, "telegram_user_id": payload["telegram_user_id"] + 1})
    with pytest.raises(AuthorizationError):
        grant(service, lead, {**payload, "telegram_user_id": payload["telegram_user_id"] + 1})
    service.set_user_role(lead, member.email, Role.LEAD)
    assert grant(service, lead, payload)["role"] == "lead"


@pytest.mark.parametrize("role", ["member", "manager", "lead"])
def test_owner_approved_roles_and_idempotent_replay(scenario, role):
    service, lead, payload = scenario
    first = grant(service, lead, {**payload, "role": role})
    assert first["role"] == role
    assert service.authenticate(payload["token"]).role.value == role
    assert grant(service, lead, {**payload, "role": "member"}) == first


def test_pinned_owner_reuses_existing_lead_without_downgrade(scenario):
    service, lead, payload = scenario
    binding = {str(payload["telegram_user_id"]): str(lead.id)}
    before = service.repository.tokens_of_user(lead.id)
    result = grant(service, lead, payload, binding)
    assert result["user_id"] == str(lead.id)
    assert result["role"] == "lead"
    assert service.authenticate(payload["token"]).id == lead.id
    assert service.repository.user_by_email(f"telegram-{payload['telegram_user_id']}@users.brainlab-ai.com") is None
    assert len(service.repository.tokens_of_user(lead.id)) == len(before) + 1
    assert grant(service, lead, payload, binding) == result


@pytest.mark.parametrize("same_label", [False, True])
def test_promotion_requires_review_of_other_existing_keys(scenario, same_label):
    service, lead, payload = scenario
    result = grant(service, lead, payload)
    service.issue_agent_token(lead, f"telegram:{payload['telegram_user_id']}" if same_label else "other-device", secrets.token_hex(32), email=result["email"])
    with pytest.raises(AuthorizationError):
        grant(service, lead, {**payload, "role": "lead"})
    assert service.authenticate(payload["token"]).role is Role.MEMBER


def test_verified_owner_repair_is_exact_and_replayable(scenario):
    import hashlib
    from repair_telegram_owner import repair_owner

    service, lead, payload = scenario
    old = grant(service, lead, payload)
    before = service.repository.tokens_of_user(lead.id)
    digest = hashlib.sha256(payload["token"].encode()).hexdigest()
    args = (payload["telegram_user_id"], old["user_id"], str(lead.id), digest)
    assert repair_owner(service, lead, *args)["changed"] is True
    assert repair_owner(service, lead, *args)["changed"] is False
    assert service.authenticate(payload["token"]).id == lead.id
    assert service.authenticate(payload["token"]).role is Role.LEAD
    assert service.repository.tokens_of_user(lead.id)[:len(before)] == before
    assert not service.repository.tokens_of_user(service.repository.user_by_email(old["email"]).id)
    assert grant(service, lead, payload, {str(payload["telegram_user_id"]): str(lead.id)})["role"] == "lead"


def test_owner_repair_refuses_wrong_hash_and_wrong_actor(scenario):
    import hashlib
    from repair_telegram_owner import repair_owner

    service, lead, payload = scenario
    old = grant(service, lead, payload)
    member = service.authenticate(payload["token"])
    digest = hashlib.sha256(payload["token"].encode()).hexdigest()
    with pytest.raises(AuthorizationError):
        repair_owner(service, lead, payload["telegram_user_id"], old["user_id"], str(lead.id), "0" * 64)
    with pytest.raises(AuthorizationError):
        repair_owner(service, member, payload["telegram_user_id"], old["user_id"], str(lead.id), digest)
    assert service.authenticate(payload["token"]).id == member.id
