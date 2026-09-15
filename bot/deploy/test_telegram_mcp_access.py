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
    with pytest.raises(AuthorizationError):
        grant(service, lead, payload)
