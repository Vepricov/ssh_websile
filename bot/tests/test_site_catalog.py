import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_public_catalog_has_separate_safe_resources() -> None:
    data = json.loads((ROOT / "servers.json").read_text())
    resources = {resource["id"]: resource for resource in data["resources"]}

    assert resources["new_server"]["kind"] == "ssh-server"
    assert resources["h200"]["vpn_contact"] == "@undef1ne5"
    assert resources["h200"]["vpn_email_sender"] == "helpdesk02@vkusvill.ru"
    assert resources["h200"]["access_contact"] == "@brainlab_server_access_bot"
    assert "@shkodnik1917" not in json.dumps(resources["h200"])
    assert resources["mlsub"]["kind"] == "compute-service"
    assert resources["mlsub"]["requires"] == ["new_server"]
    assert resources["mlsub"]["approval"] == "separate-admin-approval"
    assert resources["alibaba_ascend"]["status"] == "pilot"
    assert resources["alibaba_ascend"]["requestable"] is True
    assert resources["alibaba_ascend"]["approval"] == "admin-approved-automatic"
    assert resources["alibaba_ascend"]["vpn_contact"] == "@undef1ne5"
    assert (
        resources["alibaba_ascend"]["vpn_email_sender"]
        == "helpdesk02@vkusvill.ru"
    )
    assert resources["alibaba_ascend"]["access_steps"][:3] == [
        "Reuse the H200 VPN if it is already configured.",
        "Otherwise send full name and MIPT email to @undef1ne5 to request VPN.",
        "Wait for the VPN email from helpdesk02@vkusvill.ru.",
    ]
    assert "access_contact" not in resources["alibaba_ascend"]
    assert "creates the Unix account" in " ".join(
        resources["alibaba_ascend"]["notes"]
    )
    assert "group" not in resources["alibaba_ascend"]
    serialized = json.dumps(data).lower()
    assert "password" not in serialized
    assert "api_key" not in serialized


def test_agent_entrypoint_links_catalog_and_resource_guides() -> None:
    llms = (ROOT / "llms.txt").read_text()

    assert "/servers.json" in llms
    assert "/servers.html#mlsub" in llms
    assert "/servers.html#h200" in llms
    assert "brain_lab_vpn" not in llms
    assert "do-vpn" not in llms
    assert "@undef1ne5" in llms
    assert "helpdesk02@vkusvill.ru" in llms
    assert "@brainlab_server_access_bot" in llms
    assert "@shkodnik1917" not in llms


def test_web_form_requires_mlsub_to_be_requested_separately() -> None:
    form_logic = (ROOT / "server-form.js").read_text()

    assert 'payload.servers.includes("mlsub")' in form_logic
    assert "mlsub оформляется отдельной заявкой" in form_logic


def test_web_form_offers_ascend_as_a_separate_request() -> None:
    form = (ROOT / "server-form.html").read_text()
    form_logic = (ROOT / "server-form.js").read_text()

    assert 'name="servers" value="alibaba_ascend"' in form
    assert 'payload.servers.includes("alibaba_ascend")' in form_logic
    assert "Alibaba оформляется отдельной заявкой" in form_logic
    assert "если VPN для H200 уже настроен" in form
    assert "@undef1ne5" in form
    assert "helpdesk02@vkusvill.ru" in form
    assert "^[a-z][a-z0-9_-]{1,30}$" in form_logic
    assert "Для Alibaba ник должен" in form_logic


def test_public_pages_offer_ascend_through_the_bot() -> None:
    home = (ROOT / "index.html").read_text()
    catalog = (ROOT / "servers.html").read_text()
    agents = (ROOT / "llms.txt").read_text()

    assert 'href="servers.html#alibaba-ascend"' in home
    assert "Пилотный внешний ресурс" in catalog
    assert "автоматическая выдача" in catalog
    assert "README.txt" in catalog
    assert "Подать заявку" in catalog
    assert "тот же VPN, что и для H200" in catalog
    assert "@undef1ne5" in catalog
    assert "helpdesk02@vkusvill.ru" in catalog
    assert "testing-only" not in agents
    assert "@fckand1" not in agents
    assert "bot creates the Unix account" in agents
    assert "~/README.txt" in agents
