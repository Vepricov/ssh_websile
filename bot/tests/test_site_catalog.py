import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_public_catalog_has_separate_safe_resources() -> None:
    data = json.loads((ROOT / "servers.json").read_text())
    resources = {resource["id"]: resource for resource in data["resources"]}

    assert resources["new_server"]["kind"] == "ssh-server"
    assert resources["mlsub"]["kind"] == "compute-service"
    assert resources["alibaba_ascend"]["status"] == "testing"
    assert resources["alibaba_ascend"]["requestable"] is False
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
