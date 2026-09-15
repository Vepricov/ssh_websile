import pytest

from lab_knowledge.errors import AuthorizationError
from lab_knowledge.models import Role


@pytest.mark.parametrize("target_role", [Role.LEAD, Role.MANAGER])
@pytest.mark.parametrize("action", ["issue", "list", "revoke"])
def test_manager_cannot_manage_another_privileged_accounts_keys(scenario, target_role, action):
    service = scenario.service
    target = service.bootstrap_user("privileged@lab", target_role, "privileged-key")
    with pytest.raises(AuthorizationError):
        if action == "issue":
            service.issue_agent_token(scenario.manager, "unauthorized", "leaked-key", target.email)
        elif action == "list":
            service.list_agent_tokens(scenario.manager, target.email)
        else:
            service.revoke_agent_token(scenario.manager, "", target.email)
    assert service.authenticate("privileged-key").id == target.id


def test_manager_can_manage_member_and_own_keys(scenario):
    service = scenario.service
    member = service.invite_member(scenario.manager, "student@lab")
    for target, token in ((member, "student-key"), (scenario.manager, "self-key")):
        service.issue_agent_token(scenario.manager, "test-device", token, target.email)
        assert service.authenticate(token).id == target.id
        assert any(t["label"] == "test-device" for t in service.list_agent_tokens(scenario.manager, target.email))
        assert service.revoke_agent_token(scenario.manager, "test-device", target.email) == 1


@pytest.mark.parametrize("action", ["issue", "revoke"])
def test_manager_rechecks_role_after_acquiring_account_lock(scenario, monkeypatch, action):
    service = scenario.service
    member = service.invite_member(scenario.manager, "promotion-race@lab")
    original = service.repository.user_by_email
    calls = 0

    def lookup(email):
        nonlocal calls
        calls += 1
        result = original(email)
        if calls == 1:
            # Another transaction promoted the account after the preliminary lookup.
            from dataclasses import replace
            stale = replace(result)
            service.repository.set_user_role(member.id, Role.LEAD)
            return stale
        return result

    monkeypatch.setattr(service.repository, "user_by_email", lookup)
    with pytest.raises(AuthorizationError):
        if action == "issue":
            service.issue_agent_token(scenario.manager, "racing-key", "stale-privilege-key", member.email)
        else:
            service.revoke_agent_token(scenario.manager, "racing-key", member.email)
    assert not service.repository.tokens_of_user(member.id)
