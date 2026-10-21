import asyncio
import urllib.error

from server_access_bot import work_access as wa


def test_only_works_at_depth_three_are_listed():
    assert wa.это_работа("brainlab/low-rank/low-rank-step-geometry/lora-muon", "lora-muon")
    assert not wa.это_работа("brainlab/low-rank/low-rank-step-geometry/literature", "literature")
    assert not wa.это_работа("brainlab/gitlab-profile", "gitlab-profile")
    assert not wa.это_работа("ops/grants/a2-pro/x", "x")


def test_list_is_live_new_work_appears_after_cache(monkeypatch):
    проекты = [{"id": 1, "path": "a", "name": "a", "path_with_namespace": "brainlab/d/t/a"}]

    async def страницы(путь):
        return list(проекты)
    monkeypatch.setattr(wa, "страницы", страницы)
    monkeypatch.setattr(wa, "_работы", [])

    async def run():
        первые = await wa.все_работы()
        проекты.append({"id": 2, "path": "b", "name": "b",
                        "path_with_namespace": "brainlab/d/t/b"})
        wa._работы_до = 0.0
        return первые, await wa.все_работы()
    первые, вторые = asyncio.run(run())
    assert [р.ид for р in первые] == [1]
    assert [р.ид for р in вторые] == [1, 2]


def test_callback_data_fits_telegram_limit():
    р = wa.Работа(123456, "brainlab/visual-generative-ai/world-models/camera-control", "x")
    for ряд in wa.кнопки_решения(р, 987654, wa.РАЗРАБОТЧИК).inline_keyboard:
        for кнопка in ряд:
            assert len(кнопка.callback_data.encode()) <= 64
    for ряд in wa.меню_направлений([р]).inline_keyboard:
        assert len(ряд[0].callback_data.encode()) <= 64


def test_reporter_offered_only_as_fallback_for_developer():
    р = wa.Работа(1, "brainlab/d/t/w", "w")
    тексты = [к.text for ряд in wa.кнопки_решения(р, 2, wa.РАЗРАБОТЧИК).inline_keyboard
              for к in ряд]
    assert тексты == ["Выдать Developer", "Только Reporter", "Отказать"]
    тексты = [к.text for ряд in wa.кнопки_решения(р, 2, wa.ЧИТАТЕЛЬ).inline_keyboard
              for к in ряд]
    assert тексты == ["Выдать Reporter", "Отказать"]


def test_set_role_updates_existing_direct_membership(monkeypatch):
    вызовы = []

    async def api(путь, метод="GET", данные=None):
        вызовы.append((метод, путь, данные))
        if метод == "POST":
            raise urllib.error.HTTPError(путь, 409, "exists", {}, None)
    monkeypatch.setattr(wa, "api", api)
    asyncio.run(wa.поставить(7, 9, wa.РАЗРАБОТЧИК))
    assert вызовы[-1] == ("PUT", "projects/7/members/9", {"access_level": 30})


def test_level_is_zero_for_non_member(monkeypatch):
    async def api(путь, метод="GET", данные=None):
        raise urllib.error.HTTPError(путь, 404, "nope", {}, None)
    monkeypatch.setattr(wa, "api", api)
    assert asyncio.run(wa.уровень(7, 9)) == 0
