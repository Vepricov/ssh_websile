from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "bot" / "deploy" / "user-docs"


def test_every_managed_server_has_two_user_documents() -> None:
    common = (DOCS / "BRAINLAB_RULES.md").read_text()

    assert "CUDA_VISIBLE_DEVICES" in common
    assert "Не останавливайте чужие процессы" in common
    for host in ("shkodnik_opt", "brain_lab", "vv_h200"):
        guide = (DOCS / host / "SERVER_GUIDE.md").read_text()
        assert guide.startswith("# ")
        assert "@brainlab_server_access_bot" in guide


def test_brain_lab_guide_contains_mlsub_access_and_usage_rules() -> None:
    guide = (DOCS / "brain_lab" / "SERVER_GUIDE.md").read_text()

    assert "отдельную заявку" in guide
    assert "одобрения администратора" in guide
    assert "mlsub run" in guide
    assert "mlsub list" in guide
    assert "/home/jovyan" in guide


def test_mlsub_helper_installs_docs_before_granting_group() -> None:
    helper = (ROOT / "bot" / "deploy" / "hermes-enable-mlsub").read_text()

    copy_position = helper.index("runuser -u")
    grant_position = helper.index("usermod --append")
    assert copy_position < grant_position
    assert "cp --no-clobber" in helper
