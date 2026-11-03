from pathlib import Path

import pytest

from server_access_bot import gitlab_access


def test_extra_recommenders_reads_the_list(tmp_path: Path, monkeypatch) -> None:
    f = tmp_path / "extra-recommenders.json"
    f.write_text("[39228482, 625743920, 479170246]", encoding="utf-8")
    monkeypatch.setattr(gitlab_access, "ДОП_РЕКОМЕНДАТЕЛИ", f)
    assert gitlab_access.доп_рекомендатели() == frozenset(
        {39228482, 625743920, 479170246}
    )


@pytest.mark.parametrize("content", ["", "not json", "{}"])
def test_extra_recommenders_is_empty_on_missing_or_garbled_file(
    tmp_path: Path, monkeypatch, content: str
) -> None:
    f = tmp_path / "extra-recommenders.json"
    if content:
        f.write_text(content, encoding="utf-8")
    monkeypatch.setattr(gitlab_access, "ДОП_РЕКОМЕНДАТЕЛИ", f)
    assert gitlab_access.доп_рекомендатели() == frozenset()
