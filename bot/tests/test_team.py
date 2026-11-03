import asyncio
import base64
import sqlite3

from server_access_bot import team
from server_access_bot.work_access import Работа

README = ("# Работа\n\n**Ведут** [Ведущий Первый](https://g.example/lead)\n"
          "**Команда** [Учёткин Иван](https://g.example/ivan), Безучёткин Пётр\n")


def test_team_lines_give_names_and_logins():
    assert team.состав(README) == [team.Член("Ведущий Первый", "lead"),
                                   team.Член("Учёткин Иван", "ivan"),
                                   team.Член("Безучёткин Пётр")]


def test_person_is_found_by_login_or_by_a_single_name():
    люди = [team.Человек(1, "Пётр Безучёткин"), team.Человек(2, "Пётр Другой"),
            team.Человек(3, "Тёзка Двойной"), team.Человек(4, "Двойной Тёзка")]
    assert team.кто_это(team.Член("Учёткин Иван", "ivan"), люди, {"ivan": 9}) == 9
    assert team.кто_это(team.Член("Безучёткин Петр"), люди, {}) == 1
    assert team.кто_это(team.Член("Тёзка Двойной"), люди, {}) is None
    assert team.кто_это(team.Член("Никого Нет"), люди, {}) is None


def test_bot_people_come_from_profiles_and_server_users(tmp_path):
    база = tmp_path / "bot.sqlite3"
    with sqlite3.connect(база) as db:
        db.execute("CREATE TABLE profiles (telegram_user_id INTEGER, full_name TEXT)")
        db.execute("CREATE TABLE users (telegram_user_id INTEGER, full_name TEXT)")
        db.execute("INSERT INTO profiles VALUES (1, 'Пётр Безучёткин')")
        db.execute("INSERT INTO users VALUES (2, 'Иван Учёткин')")
    assert team.люди_бота(база) == [team.Человек(1, "Пётр Безучёткин"),
                                    team.Человек(2, "Иван Учёткин")]


class Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, кому, текст, **_):
        self.sent.append((кому, текст))


def test_pass_writes_new_members_once_and_opens_access_later(tmp_path, monkeypatch):
    monkeypatch.setattr(team, "СОСТОЯНИЕ", tmp_path / "state.json")
    работа = Работа(7, "brainlab/d/t/w", "w")
    текст = {"readme": "# W\n\n**Ведут** [Ведущий Первый](https://g.example/lead)\n"}
    карта = {"lead": 5}
    открыто = []

    async def все_работы():
        return [работа]

    async def readme(_):
        return текст["readme"]

    async def открыть(_, логин):
        открыто.append(логин)
        return логин != "missing"

    monkeypatch.setattr(team, "все_работы", все_работы)
    monkeypatch.setattr(team, "readme", readme)
    monkeypatch.setattr(team, "открыть", открыть)
    monkeypatch.setattr(team, "прочитать_карту", lambda: dict(карта))
    monkeypatch.setattr(team, "люди_бота", lambda: [team.Человек(1, "Пётр Безучёткин")])
    bot = Bot()

    assert asyncio.run(team.проход(bot)) == 0  # работа впервые: только запомнить
    текст["readme"] += "**Команда** Безучёткин Пётр\n"
    assert asyncio.run(team.проход(bot)) == 1
    assert bot.sent[0][0] == 1 and "Учётки в базе знаний у вас пока нет" in bot.sent[0][1]
    assert asyncio.run(team.проход(bot)) == 0  # ждёт учётку, не напоминаем
    карта["petr"] = 1
    assert asyncio.run(team.проход(bot)) == 1
    assert "Доступ к работе в базе знаний открыт" in bot.sent[1][1]
    assert открыто[-1] == "petr"
    assert asyncio.run(team.проход(bot)) == 0


def test_readme_is_decoded_from_the_files_api(monkeypatch):
    async def api(путь):
        assert путь == "projects/7/repository/files/README.md?ref=main"
        return {"content": base64.b64encode(README.encode()).decode()}

    monkeypatch.setattr(team, "api", api)
    assert asyncio.run(team.readme(Работа(7, "brainlab/d/t/w", "w"))) == README
