"""Команда работы: кого записали в «Ведут» или «Команду» README, тому бот пишет сам.

Человека записывают в команду из окна созвона или правкой README, а узнавал он об этом
случайно. У кого учётки в базе ещё нет, тот и в работу попасть не мог: доступ к серверам
у него был, к GitLab нет. Владелец 03-11-2026: «например доступ к GitLab нет, а доступ к боту
есть, я пишу условно его имя, ему в telegram боте приходит уведомление: вот вас записали в
команду такого-то проекта, получите доступ к GitLab и к этому проекту».

Проход раз в несколько минут читает README работ в `main`. Новый человек в команде узнаётся
по ссылке на учётку, а без неё — по имени и фамилии из профиля бота, только если совпадение
одно. С учёткой он сразу становится разработчиком работы и получает письмо. Без учётки
получает письмо с тем, как её получить, и ждёт: когда учётку выдадут, доступ к работе
откроется на следующем проходе, и придёт второе письмо.

Работа, которую проход видит впервые, только запоминается: иначе после выкладки письмо
получил бы каждый, кто давно в команде, а новая работа — весь свой состав вместе с ведущим.

`python -m server_access_bot.team --people` печатает людей бота (имя и логин, если есть)
для поиска в окне созвона Brain Call. Телеграм-идентификаторы туда не попадают.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import html
import json
import logging
import os
import re
import sqlite3
import urllib.error
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot

from .gitlab_access import РАЗРАБОТЧИК, api, включено, прочитать_карту
from .work_access import Работа, все_работы, поставить, пользователь, уровень

logger = logging.getLogger(__name__)

СОСТОЯНИЕ = Path(os.environ.get("LAB_TEAM_STATE", "/var/lib/server-access-bot/team-seen.json"))
БАЗА = Path(os.environ.get("ACCESS_BOT_DATABASE", "/var/lib/server-access-bot/bot.sqlite3"))
СТРОКИ = ("**Ведут**", "**Ведёт**", "**Команда**")
ПРОХОД_РАЗ_В = 300
_ССЫЛКА = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


@dataclass(frozen=True)
class Член:
    имя: str
    логин: str = ""


@dataclass(frozen=True)
class Человек:
    телеграм: int
    имя: str


def ключ(имя: str) -> frozenset[str]:
    """Имя как набор слов: «Имя Фамилия» и «Фамилия Имя» совпадают, ё = е."""
    return frozenset(w for w in re.findall(r"\w+", имя.casefold().replace("ё", "е"))
                     if len(w) > 1)


def состав(readme: str) -> list[Член]:
    """Люди строк «Ведут» и «Команда»: ссылка на учётку или просто имя."""
    члены: list[Член] = []
    for строка in readme.splitlines():
        начало = next((с for с in СТРОКИ if строка.startswith(с)), None)
        if начало is None:
            continue
        for кусок in строка[len(начало):].split(","):
            кусок = кусок.strip()
            ссылка = _ССЫЛКА.search(кусок)
            if ссылка:
                логин = ссылка.group(2).rstrip("/").rsplit("/", 1)[-1]
                члены.append(Член(ссылка.group(1).strip(), логин))
            elif ключ(кусок):
                члены.append(Член(" ".join(кусок.split())))
    return члены


def люди_бота(база: Path = БАЗА) -> list[Человек]:
    """Все, кто заполнял профиль или брал сервер: телеграм и имя."""
    try:
        with sqlite3.connect(f"file:{база}?mode=ro", uri=True) as db:
            строки = db.execute(
                "SELECT telegram_user_id, full_name FROM profiles "
                "UNION SELECT telegram_user_id, full_name FROM users").fetchall()
    except sqlite3.Error as беда:
        logger.warning("люди бота не прочитаны: %s", беда)
        return []
    по_телеграму = {int(т): str(и) for т, и in строки if т and и and ключ(str(и))}
    return [Человек(т, и) for т, и in sorted(по_телеграму.items())]


def кто_это(член: Член, люди: list[Человек], карта: dict[str, int]) -> int | None:
    """Телеграм члена команды: по логину, иначе по имени — только единственное совпадение."""
    if член.логин and член.логин in карта:
        return карта[член.логин]
    нужно = ключ(член.имя)
    совпали = {ч.телеграм for ч in люди if len(нужно & ключ(ч.имя)) >= 2}
    return совпали.pop() if len(совпали) == 1 else None


def _прочитать() -> dict:
    try:
        сырое = json.loads(СОСТОЯНИЕ.read_text(encoding="utf-8"))
        return сырое if isinstance(сырое, dict) else {}
    except (OSError, ValueError):
        return {}


def _записать(состояние: dict) -> None:
    try:
        СОСТОЯНИЕ.parent.mkdir(parents=True, exist_ok=True)
        СОСТОЯНИЕ.write_text(json.dumps(состояние, ensure_ascii=False), encoding="utf-8")
    except OSError as беда:
        logger.warning("не записать составы команд: %s", беда)


async def readme(работа_: Работа) -> str:
    файл = await api(f"projects/{работа_.ид}/repository/files/README.md?ref=main")
    return base64.b64decode(str((файл or {}).get("content") or "")).decode("utf-8", "replace")


async def открыть(работа_: Работа, логин: str) -> bool:
    """Разработчик работы, если ещё нет такого доступа. False — учётки нет."""
    кто = await пользователь(логин)
    if not кто:
        return False
    if await уровень(работа_.ид, int(кто["id"])) < РАЗРАБОТЧИК:
        await поставить(работа_.ид, int(кто["id"]), РАЗРАБОТЧИК)
    return True


def письмо(работа_: Работа, открыто: bool) -> str:
    шапка = (f"Вас записали в команду работы <b>{html.escape(работа_.имя)}</b>:\n"
             f"{работа_.адрес}")
    if открыто:
        return шапка + "\n\nДоступ к работе в базе знаний открыт."
    return (шапка + "\n\nУчётки в базе знаний у вас пока нет. Нажмите «🧠 База знаний MCP» "
            "внизу и заполните заявку: когда учётку выдадут, доступ к этой работе откроется "
            "сам.")


async def проход(bot: Bot) -> int:
    """Один обход работ. Возвращает число отправленных писем."""
    состояние = _прочитать()
    видели: dict = состояние.setdefault("видели", {})
    ждут: dict = состояние.setdefault("ждут", {})
    люди = await asyncio.to_thread(люди_бота)
    карта = прочитать_карту()
    отправлено = 0
    for работа_ in await все_работы():
        try:
            члены = состав(await readme(работа_))
        except (urllib.error.HTTPError, urllib.error.URLError, ValueError):
            continue
        ид = str(работа_.ид)
        прежние = set(видели.get(ид) or [])
        видели[ид] = sorted({ч.имя for ч in члены})
        if ид not in состояние.get("начато", []):
            continue
        for член in члены:
            новый = член.имя not in прежние
            ждёт = член.имя in (ждут.get(ид) or [])
            if not новый and not ждёт:
                continue
            телеграм = кто_это(член, люди, карта)
            логин = член.логин or next((л for л, т in карта.items() if т == телеграм), "")
            try:
                открыто = bool(логин) and await открыть(работа_, логин)
            except (urllib.error.HTTPError, urllib.error.URLError) as беда:
                logger.warning("доступ к %s для %s не открыт: %s", работа_.путь, логин, беда)
                continue
            if телеграм is None:
                # В боте его пока нет: доступ открыт молча, а без учётки он ждёт, пока
                # не появится в боте и не получит её.
                if not открыто:
                    ждут[ид] = sorted({*(ждут.get(ид) or []), член.имя})
                continue
            if ждёт and not открыто:
                continue
            try:
                await bot.send_message(телеграм, письмо(работа_, открыто),
                                       disable_web_page_preview=True)
                отправлено += 1
            except Exception as беда:  # noqa: BLE001 — один адресат не должен ломать проход
                logger.warning("письмо о команде %s не доставлено: %s", работа_.путь, беда)
                continue
            очередь = set(ждут.get(ид) or [])
            if открыто:
                очередь.discard(член.имя)
            else:
                очередь.add(член.имя)
            ждут[ид] = sorted(очередь)
    состояние["начато"] = sorted(видели)
    _записать(состояние)
    return отправлено


async def run_team_loop(bot: Bot, interval_seconds: int = ПРОХОД_РАЗ_В) -> None:
    if not включено():
        return
    while True:
        try:
            отправлено = await проход(bot)
            if отправлено:
                logger.info("писем о команде: %s", отправлено)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — проход повторится, бот работает дальше
            logger.exception("проход по командам упал")
        await asyncio.sleep(interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--people", action="store_true", help="люди бота в JSON")
    if not parser.parse_args().people:
        parser.error("нужен --people")
    карта = прочитать_карту()
    логины = {т: л for л, т in карта.items()}
    print(json.dumps([{"name": ч.имя, "login": логины.get(ч.телеграм, "")}
                      for ч in люди_бота()], ensure_ascii=False))


if __name__ == "__main__":
    main()


__all__ = ["Член", "Человек", "кто_это", "люди_бота", "письмо", "проход", "run_team_loop",
           "состав"]
