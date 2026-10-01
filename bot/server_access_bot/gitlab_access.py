"""Заявки на доступ в GitLab: приходят в телеграм, решаются кнопкой.

Почта на сервере базы выключена нарочно, поэтому заявка на доступ никому не приходит: её
видно только в интерфейсе у владельца группы. Это и значит, что заявка не работает —
человек нажал кнопку и ждёт неизвестно чего.

Здесь заявка доводится до людей. Проход опрашивает GitLab и присылает заявку тем, кто за
это пространство отвечает: ведущим и лидам самой группы или работы, а также владельцу
лаборатории. Решение принимается кнопкой, и уровень выбирается сразу:

    читать      Reporter    видит работы темы, писать не может
    участник    Developer   пушит ветки и открывает предложения
    ведущий     Maintainer  сливает предложения

Выключено, если нет `LAB_GITLAB_TOKEN`: без ключа опрашивать нечего, и это не поломка.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)

АДРЕС = os.environ.get("LAB_GITLAB_URL", "http://127.0.0.1:9080").rstrip("/")
ПУБЛИЧНЫЙ = os.environ.get("LAB_GITLAB_PUBLIC_URL", "").rstrip("/") or АДРЕС
#: Служба запущена с `ProtectSystem=strict`, и писать ей можно только в
#: `/var/lib/server-access-bot`. В `/opt` запись отвергается как read-only — проверено.
СОСТОЯНИЕ = Path(os.environ.get("LAB_GITLAB_STATE",
                                "/var/lib/server-access-bot/gitlab-requests.json"))
#: Карта «логин в GitLab → телеграм». Ключ группы чужую почту не видит и видеть не должен,
#: поэтому связь ведёт сам бот: он же и заводит учётку, и знает в этот момент обоих.
КАРТА = Path(os.environ.get("LAB_GITLAB_PEOPLE",
                            "/var/lib/server-access-bot/gitlab-people.json"))
#: Почта, которую выдаёт база телеграм-участнику: из неё узнаётся, кому писать.
ПОДСТАВНАЯ = "@users.brainlab-ai.com"
ПРИСТАВКА = "telegram-"
#: Уровни GitLab.
ЧИТАТЕЛЬ, РАЗРАБОТЧИК, ВЕДУЩИЙ = 20, 30, 40
НАЗВАНИЕ_УРОВНЯ = {ЧИТАТЕЛЬ: "читатель", РАЗРАБОТЧИК: "участник", ВЕДУЩИЙ: "ведущий"}
#: Кто имеет право решать: ведущий и выше.
РЕШАЮТ_ОТ = ВЕДУЩИЙ


def включено() -> bool:
    return bool(os.environ.get("LAB_GITLAB_TOKEN", "").strip())


def _запрос(путь: str, метод: str = "GET", данные: dict | None = None):
    тело = json.dumps(данные).encode("utf-8") if данные is not None else None
    адрес = urllib.parse.quote(путь, safe="/?&=%#:+,.~*!$'()[]@-_")
    запрос = urllib.request.Request(
        f"{АДРЕС}/api/v4/{адрес}", data=тело, method=метод,
        headers={"PRIVATE-TOKEN": os.environ.get("LAB_GITLAB_TOKEN", ""),
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(запрос, timeout=60) as ответ:
        сырое = ответ.read().decode("utf-8")
    return json.loads(сырое) if сырое.strip() else None


async def api(путь: str, метод: str = "GET", данные: dict | None = None):
    return await asyncio.to_thread(_запрос, путь, метод, данные)


async def страницы(путь: str) -> list:
    собрано, стр = [], 1
    while True:
        склейка = "&" if "?" in путь else "?"
        порция = await api(f"{путь}{склейка}per_page=100&page={стр}")
        порция = порция if isinstance(порция, list) else []
        собрано += порция
        if len(порция) < 100:
            return собрано
        стр += 1


@dataclass(frozen=True)
class Заявка:
    вид: str          # groups или projects
    ид: int
    путь: str         # человеческое имя пространства
    адрес: str        # ссылка наружу
    кто: int          # id пользователя GitLab
    логин: str
    имя: str
    телеграм: int | None

    @property
    def ключ(self) -> str:
        return f"{self.вид}:{self.ид}:{self.кто}"


def телеграм_из_почты(почта: str) -> int | None:
    """Телеграм-идентификатор из почты, которую база выдала участнику."""
    если = (почта or "").strip().lower()
    if not если.endswith(ПОДСТАВНАЯ) or not если.startswith(ПРИСТАВКА):
        return None
    номер = если[len(ПРИСТАВКА):-len(ПОДСТАВНАЯ)]
    return int(номер) if номер.isdigit() else None


def запомнить_логин(логин: str, телеграм: int) -> None:
    """Связать логин GitLab с телеграмом. Зовётся при выдаче доступа."""
    if not логин or not телеграм:
        return
    карта = прочитать_карту()
    if карта.get(логин) == телеграм:
        return
    карта[логин] = телеграм
    try:
        КАРТА.parent.mkdir(parents=True, exist_ok=True)
        КАРТА.write_text(json.dumps(карта, ensure_ascii=False, indent=1),
                         encoding="utf-8")
        КАРТА.chmod(0o600)
    except OSError as беда:
        logger.warning("не записать карту людей GitLab: %s", беда)


def прочитать_карту() -> dict[str, int]:
    try:
        сырое = json.loads(КАРТА.read_text(encoding="utf-8"))
        return {str(к): int(v) for к, v in сырое.items()}
    except (OSError, ValueError):
        return {}


async def телеграм_кого(ид: int, логин: str = "") -> int | None:
    """Телеграм участника: по почте, если она видна, иначе по карте бота."""
    кто = None
    try:
        кто = await api(f"users/{ид}")
    except (urllib.error.HTTPError, urllib.error.URLError):
        кто = None
    если = телеграм_из_почты(str((кто or {}).get("email") or ""))
    if если:
        return если
    ник = логин or str((кто or {}).get("username") or "")
    return прочитать_карту().get(ник)


def _прочитать_состояние() -> set[str]:
    try:
        return set(json.loads(СОСТОЯНИЕ.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def _записать_состояние(ключи: set[str]) -> None:
    try:
        СОСТОЯНИЕ.parent.mkdir(parents=True, exist_ok=True)
        СОСТОЯНИЕ.write_text(json.dumps(sorted(ключи), ensure_ascii=False),
                             encoding="utf-8")
    except OSError as беда:
        logger.warning("не записать состояние заявок GitLab: %s", беда)


#: Куда можно постучаться. Список меняется редко, а обход его стоит под сотню запросов,
#: поэтому он держится в памяти. Открытое (литература, передние страницы) пропускается: в
#: него не просятся, оно и так видно всем, кто вошёл.
ОТКРЫТОЕ = ("literature", "gitlab-profile")
_места: list[tuple[str, int, str, str]] = []
_места_до = 0.0
МЕСТА_ЖИВУТ = 600


async def места_заявок(сейчас: float) -> list[tuple[str, int, str, str]]:
    global _места, _места_до
    if _места and сейчас < _места_до:
        return _места
    собрано: list[tuple[str, int, str, str]] = []
    for г in await страницы("groups"):
        собрано.append(("groups", г["id"], г.get("name") or г["full_path"],
                        f"{ПУБЛИЧНЫЙ}/groups/{г['full_path']}"))
    for п in await страницы("projects?archived=false"):
        if п.get("path") in ОТКРЫТОЕ:
            continue
        собрано.append(("projects", п["id"], п.get("name") or п["path_with_namespace"],
                        f"{ПУБЛИЧНЫЙ}/{п['path_with_namespace']}"))
    _места, _места_до = собрано, сейчас + МЕСТА_ЖИВУТ
    return собрано


async def собрать_заявки() -> list[Заявка]:
    """Все ждущие заявки: и на группы, и на проекты."""
    места = await места_заявок(asyncio.get_running_loop().time())
    вышло: list[Заявка] = []
    for вид, ид, имя_места, адрес in места:
        try:
            строки = await страницы(f"{вид}/{ид}/access_requests")
        except (urllib.error.HTTPError, urllib.error.URLError):
            continue
        for с in строки:
            логин = str(с.get("username") or "")
            вышло.append(Заявка(
                вид=вид, ид=ид, путь=имя_места, адрес=адрес, кто=int(с["id"]),
                логин=логин, имя=str(с.get("name") or ""),
                телеграм=await телеграм_кого(int(с["id"]), логин)))
    return вышло


async def кому_решать(заявка: Заявка, владельцы: frozenset[int]) -> set[int]:
    """Ведущие и лиды этого пространства плюс владелец лаборатории."""
    адресаты: set[int] = set(владельцы)
    try:
        члены = await страницы(f"{заявка.вид}/{заявка.ид}/members/all")
    except (urllib.error.HTTPError, urllib.error.URLError):
        члены = []
    for м in члены:
        if int(м.get("access_level") or 0) < РЕШАЮТ_ОТ:
            continue
        если = await телеграм_кого(int(м["id"]), str(м.get("username") or ""))
        if если:
            адресаты.add(если)
    return адресаты


def кнопки(заявка: Заявка) -> InlineKeyboardMarkup:
    пара = f"{заявка.вид[0]}:{заявка.ид}:{заявка.кто}"
    строки = [[
        InlineKeyboardButton(text="Читать", callback_data=f"gl:a:{пара}:{ЧИТАТЕЛЬ}"),
        InlineKeyboardButton(text="Участник",
                             callback_data=f"gl:a:{пара}:{РАЗРАБОТЧИК}"),
        InlineKeyboardButton(text="Ведущий", callback_data=f"gl:a:{пара}:{ВЕДУЩИЙ}"),
    ], [InlineKeyboardButton(text="Отказать", callback_data=f"gl:d:{пара}:0")]]
    return InlineKeyboardMarkup(inline_keyboard=строки)


def текст_заявки(заявка: Заявка) -> str:
    кто = заявка.имя or заявка.логин
    куда = ("хочет в команду работы" if заявка.вид == "projects"
            else "просит доступ к теме")
    return (f"<b>Заявка на доступ в базу знания</b>\n\n"
            f"{кто} (<code>{заявка.логин}</code>) {куда} <b>{заявка.путь}</b>\n"
            f'<a href="{заявка.адрес}">открыть в браузере</a>\n\n'
            f"Читать — видит работы, писать не может. Участник — пушит ветки и открывает "
            f"предложения. Ведущий — сливает.")


async def run_gitlab_access_loop(bot: Bot, владельцы: frozenset[int],
                                 interval_seconds: int = 180) -> None:
    """Опрашивать GitLab и присылать новые заявки тем, кто их решает."""
    if not включено():
        logger.info("заявки GitLab выключены: нет LAB_GITLAB_TOKEN")
        return
    известные = _прочитать_состояние()
    while True:
        try:
            заявки = await собрать_заявки()
            живые = {з.ключ for з in заявки}
            for заявка in заявки:
                if заявка.ключ in известные:
                    continue
                адресаты = await кому_решать(заявка, владельцы)
                отправлено = False
                for кому in адресаты:
                    try:
                        await bot.send_message(кому, текст_заявки(заявка),
                                               reply_markup=кнопки(заявка))
                        отправлено = True
                    except Exception as беда:  # noqa: BLE001 — один адресат не должен
                        logger.warning("заявку %s не доставить %s: %s",
                                       заявка.ключ, кому, беда)
                if отправлено:
                    известные.add(заявка.ключ)
            # Решённые заявки забываются, иначе состояние растёт вечно.
            известные &= живые
            _записать_состояние(известные)
        except Exception:  # noqa: BLE001 — проход не должен падать и уносить бота
            logger.warning("проход заявок GitLab не отработал", exc_info=True)
        await asyncio.sleep(interval_seconds)


def build_gitlab_router(владельцы: frozenset[int]) -> Router:
    router = Router(name="gitlab-access")

    @router.callback_query(F.data.startswith("gl:"))
    async def решить(callback: CallbackQuery) -> None:
        # Кнопка несёт `gl:<a|d>:<g|p>:<номер>:<кто>:<уровень>` — шесть частей, и больше в
        # callback_data класть нельзя: у телеграма предел 64 байта.
        части = (callback.data or "").split(":")
        if len(части) != 6 or callback.from_user is None:
            await callback.answer("непонятная кнопка")
            return
        действие, вид_кратко = части[1], части[2]
        ид, кто, уровень = int(части[3]), int(части[4]), int(части[5])
        вид = "groups" if вид_кратко == "g" else "projects"
        if not await право_решать(вид, ид, callback.from_user.id, владельцы):
            await callback.answer("это решает ведущий этого пространства", show_alert=True)
            return
        try:
            заявки = await страницы(f"{вид}/{ид}/access_requests")
            if not any(int(з["id"]) == кто for з in заявки):
                await callback.answer("заявка уже решена")
                await callback.message.edit_reply_markup(reply_markup=None)
                return
            if действие == "d":
                await api(f"{вид}/{ид}/access_requests/{кто}", "DELETE")
                итог = "отказано"
            else:
                await api(f"{вид}/{ид}/access_requests/{кто}/approve", "PUT")
                await api(f"{вид}/{ид}/members/{кто}", "PUT",
                          {"access_level": уровень})
                итог = f"дано: {НАЗВАНИЕ_УРОВНЯ.get(уровень, уровень)}"
        except Exception as беда:  # noqa: BLE001 — показать человеку, а не промолчать
            logger.warning("заявка %s/%s не решена: %s", вид, ид, беда)
            await callback.answer("GitLab не принял решение, посмотри журнал",
                                  show_alert=True)
            return
        await callback.message.edit_text(
            (callback.message.html_text or "")
            + f"\n\n<b>{итог}</b> · {callback.from_user.full_name}",
            reply_markup=None)
        await callback.answer(итог)
        # Человеку, который просил, сказать о решении: иначе он ждёт молча.
        если_телеграм = await телеграм_кого(кто)
        if если_телеграм:
            try:
                await callback.bot.send_message(
                    если_телеграм, "Заявка на доступ в базу знания решена: " + итог)
            except Exception as беда:  # noqa: BLE001
                logger.warning("не сказать о решении %s: %s", если_телеграм, беда)
    return router


async def право_решать(вид: str, ид: int, телеграм: int,
                       владельцы: frozenset[int]) -> bool:
    if телеграм in владельцы:
        return True
    try:
        члены = await страницы(f"{вид}/{ид}/members/all")
    except (urllib.error.HTTPError, urllib.error.URLError):
        return False
    for м in члены:
        if int(м.get("access_level") or 0) < РЕШАЮТ_ОТ:
            continue
        if await телеграм_кого(int(м["id"]), str(м.get("username") or "")) == телеграм:
            return True
    return False
