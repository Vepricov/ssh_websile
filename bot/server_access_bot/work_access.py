"""Доступ к отдельной работе: человек просит в боте, ведущий решает кнопкой.

Гость в корне базы попроситься в работу через GitLab не может: кнопка «Request access»
видна только тому, кто ещё не член группы, а гость уже член по наследству. Повысить его
можно только приглашением в саму работу, и это долго искать. Владелец 21-10-2026: «хочу
сделать повышение от геста до девелопера более удобным … это была не статичная запись всех
проектов, а чтобы она постоянно обновлялась».

Поэтому список работ берётся из GitLab при показе (с коротким кешем): новая работа
появляется в нём сама. Заявка идёт тем же, кому идёт заявка из GitLab, — ведущему работы
из строки «Ведут» её README, затем главным темы, затем владельцам (`кому_решать`).
Роль ставит бот своим ключом, проверив, что нажавший вправе решать в этой работе.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import urllib.error
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .gitlab_access import (КОРЕНЬ, ОТКРЫТОЕ, ПУБЛИЧНЫЙ, ЧИТАТЕЛЬ, РАЗРАБОТЧИК, ВЕДУЩИЙ,
                            Заявка, api, кому_решать, право_решать, прочитать_карту,
                            страницы, телеграм_кого)

logger = logging.getLogger(__name__)

WORKS_BUTTON = "📂 Доступ к работам"
#: Уровни GitLab по-человечески: как они названы на странице состава, плюс зачем.
УРОВНИ = {
    10: "Guest",
    ЧИТАТЕЛЬ: "Reporter",
    РАЗРАБОТЧИК: "Developer",
    ВЕДУЩИЙ: "Maintainer",
    50: "Owner",
}
ЗАЧЕМ = {
    ЧИТАТЕЛЬ: "читать работу целиком",
    РАЗРАБОТЧИК: "работать: ветки и предложения",
}
ПО_СЛОВУ = {"guest": 10, "reporter": ЧИТАТЕЛЬ, "developer": РАЗРАБОТЧИК, "dev": РАЗРАБОТЧИК,
            "maintainer": ВЕДУЩИЙ}
#: Ждущие заявки, чтобы повторное нажатие не будило ведущего второй раз.
ЖДУТ = Path(os.environ.get("LAB_WORK_REQUESTS",
                           "/var/lib/server-access-bot/work-requests.json"))
РАБОТЫ_ЖИВУТ = 300
_работы: list["Работа"] = []
_работы_до = 0.0


@dataclass(frozen=True)
class Работа:
    ид: int
    путь: str        # brainlab/<направление>/<тема>/<работа>
    имя: str

    @property
    def направление(self) -> str:
        return self.путь.split("/")[1]

    @property
    def тема(self) -> str:
        return self.путь.split("/")[2]

    @property
    def адрес(self) -> str:
        return f"{ПУБЛИЧНЫЙ}/{self.путь}"


def это_работа(путь: str, слаг: str) -> bool:
    """Работа лежит ровно на глубине `корень/направление/тема/работа`."""
    части = путь.split("/")
    return len(части) == 4 and части[0] == КОРЕНЬ and слаг not in ОТКРЫТОЕ


async def все_работы() -> list[Работа]:
    global _работы, _работы_до
    сейчас = asyncio.get_running_loop().time()
    if _работы and сейчас < _работы_до:
        return _работы
    проекты = await страницы(f"groups/{КОРЕНЬ}/projects?include_subgroups=true&archived=false")
    _работы = sorted(
        (Работа(int(п["id"]), п["path_with_namespace"], п.get("name") or п["path"])
         for п in проекты if это_работа(п["path_with_namespace"], п.get("path") or "")),
        key=lambda р: р.путь)
    _работы_до = сейчас + РАБОТЫ_ЖИВУТ
    return _работы


async def работа(ид: int) -> Работа | None:
    return next((р for р in await все_работы() if р.ид == ид), None)


def логин_по_телеграму(телеграм: int) -> str:
    return next((логин for логин, кто in прочитать_карту().items() if кто == телеграм), "")


async def пользователь(логин: str) -> dict | None:
    люди = await страницы(f"users?username={логин}") if логин else []
    return люди[0] if люди else None


async def уровень(ид_работы: int, ид_человека: int) -> int:
    """Действующий уровень в работе, с наследованным из корня и темы. 0 — не член."""
    try:
        член = await api(f"projects/{ид_работы}/members/all/{ид_человека}")
    except urllib.error.HTTPError as беда:
        if беда.code == 404:
            return 0
        raise
    return int((член or {}).get("access_level") or 0)


async def поставить(ид_работы: int, ид_человека: int, уровень_: int) -> None:
    """Прямое членство в работе: добавить, а если уже есть прямое — поменять."""
    try:
        await api(f"projects/{ид_работы}/members", "POST",
                  {"user_id": ид_человека, "access_level": уровень_})
    except urllib.error.HTTPError as беда:
        if беда.code != 409:
            raise
        await api(f"projects/{ид_работы}/members/{ид_человека}", "PUT",
                  {"access_level": уровень_})


def _ждут() -> dict[str, int]:
    try:
        return {str(к): int(v) for к, v in json.loads(ЖДУТ.read_text("utf-8")).items()}
    except (OSError, ValueError):
        return {}


def _записать_ждут(ждут: dict[str, int]) -> None:
    try:
        ЖДУТ.parent.mkdir(parents=True, exist_ok=True)
        ЖДУТ.write_text(json.dumps(ждут), encoding="utf-8")
    except OSError as беда:
        logger.warning("не записать заявки в работы: %s", беда)


ПОЯСНЕНИЕ = (
    "<b>Доступ к работам</b>\n\n"
    "Вход в базу даёт роль во всей лаборатории сразу. Если в какой-то работе нужно больше — "
    "читать её целиком или работать в ней, — попросите здесь: выберите направление, потом "
    "работу и роль. Заявка уйдёт ведущему этой работы, а когда он нажмёт «Выдать», бот "
    "пришлёт вам ссылку.\n\n"
    "<b>Reporter</b> — читать работу целиком: утверждения, прогоны, рукописи.\n"
    "<b>Developer</b> — работать: ветки и предложения о слиянии. Сливать может только "
    "ведущий.\n\n"
    "Список берётся из базы каждый раз, поэтому новая работа появляется в нём сама."
)


def меню_направлений(работы: list[Работа]) -> InlineKeyboardMarkup:
    направления = sorted({р.направление for р in работы})
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=н, callback_data=f"wa:n:{н}")] for н in направления])


def меню_работ(работы: list[Работа], направление: str) -> InlineKeyboardMarkup:
    ряды = [[InlineKeyboardButton(text=f"{р.тема} · {р.имя}", callback_data=f"wa:w:{р.ид}")]
            for р in работы if р.направление == направление]
    ряды.append([InlineKeyboardButton(text="← Направления", callback_data="wa:start")])
    return InlineKeyboardMarkup(inline_keyboard=ряды)


def текст_заявки(кто: str, логин: str, р: Работа, сейчас: int, просит: int) -> str:
    return (f"<b>Заявка в работу</b>\n\n"
            f"{html.escape(кто)} (<code>{html.escape(логин)}</code>) просит "
            f"<b>{УРОВНИ[просит]}</b> в работе "
            f'<a href="{html.escape(р.адрес, quote=True)}">{html.escape(р.путь)}</a>.\n'
            f"Сейчас: {УРОВНИ.get(сейчас, 'не член')}.\n\n"
            f"<b>Reporter</b> — читает работу целиком, писать не может.\n"
            f"<b>Developer</b> — ветки и предложения, сливать не может.")


def кнопки_решения(р: Работа, кто: int, просит: int) -> InlineKeyboardMarkup:
    ряд = [InlineKeyboardButton(text=f"Выдать {УРОВНИ[просит]}",
                                callback_data=f"wa:a:{р.ид}:{кто}:{просит}")]
    if просит > ЧИТАТЕЛЬ:
        ряд.append(InlineKeyboardButton(text="Только Reporter",
                                        callback_data=f"wa:a:{р.ид}:{кто}:{ЧИТАТЕЛЬ}"))
    return InlineKeyboardMarkup(inline_keyboard=[ряд, [
        InlineKeyboardButton(text="Отказать", callback_data=f"wa:d:{р.ид}:{кто}")]])


def build_work_access_router(владельцы: frozenset[int],
                             логин_из_заявки=lambda телеграм: "") -> Router:
    """`логин_из_заявки` — запасной путь от телеграма к логину: заявка в базу его помнит."""
    router = Router(name="work-access")
    router.message.filter(F.chat.type == ChatType.PRIVATE)

    async def кто_я(телеграм: int) -> dict | None:
        return await пользователь(логин_по_телеграму(телеграм) or логин_из_заявки(телеграм))

    @router.message(Command("works"))
    @router.message(F.text == WORKS_BUTTON)
    async def начало(message: Message) -> None:
        работы = await все_работы()
        await message.answer(ПОЯСНЕНИЕ, reply_markup=меню_направлений(работы),
                             disable_web_page_preview=True)

    @router.callback_query(F.data == "wa:start")
    async def снова(callback: CallbackQuery) -> None:
        await callback.answer()
        await callback.message.edit_text(ПОЯСНЕНИЕ,
                                         reply_markup=меню_направлений(await все_работы()),
                                         disable_web_page_preview=True)

    @router.callback_query(F.data.startswith("wa:n:"))
    async def направление(callback: CallbackQuery) -> None:
        н = (callback.data or "")[5:]
        await callback.answer()
        await callback.message.edit_text(
            f"<b>{html.escape(н)}</b>: выберите работу.",
            reply_markup=меню_работ(await все_работы(), н))

    @router.callback_query(F.data.startswith("wa:w:"))
    async def выбрана(callback: CallbackQuery) -> None:
        р = await работа(int((callback.data or "")[5:]))
        if р is None:
            await callback.answer("такой работы больше нет", show_alert=True)
            return
        await callback.answer()
        я = await кто_я(callback.from_user.id)
        if я is None:
            await callback.message.answer(
                "Сначала нужен вход в базу знания: кнопка «🧠 База знаний» в меню бота. "
                "После него можно просить роль в отдельных работах.")
            return
        сейчас = await уровень(р.ид, int(я["id"]))
        ссылка = f'<a href="{html.escape(р.адрес, quote=True)}">{html.escape(р.путь)}</a>'
        выше = [у for у in (ЧИТАТЕЛЬ, РАЗРАБОТЧИК) if у > сейчас]
        if not выше:
            await callback.message.answer(
                f"В {ссылка} у вас уже {УРОВНИ.get(сейчас, сейчас)}: можно работать.",
                disable_web_page_preview=True)
            return
        await callback.message.answer(
            f"{ссылка}\nСейчас у вас: {УРОВНИ.get(сейчас, 'нет доступа')}. Что нужно?",
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=f"{УРОВНИ[у]} — {ЗАЧЕМ[у]}",
                                     callback_data=f"wa:r:{р.ид}:{у}")] for у in выше]))

    @router.callback_query(F.data.startswith("wa:r:"))
    async def попросить(callback: CallbackQuery, bot: Bot) -> None:
        _, _, ид, просит = (callback.data or "").split(":")
        р, просит = await работа(int(ид)), int(просит)
        я = await кто_я(callback.from_user.id)
        if р is None or я is None or просит not in ЗАЧЕМ:
            await callback.answer("заявку не подать", show_alert=True)
            return
        ключ = f"{р.ид}:{я['id']}"
        ждут = _ждут()
        if ждут.get(ключ, 0) >= просит:
            await callback.answer("заявка уже у ведущего, ждём решения", show_alert=True)
            return
        сейчас = await уровень(р.ид, int(я["id"]))
        заявка = Заявка(вид="projects", ид=р.ид, путь=р.путь, адрес=р.адрес,
                        кто=int(я["id"]), логин=я["username"], имя=я.get("name") or "",
                        телеграм=callback.from_user.id)
        отправлено = 0
        for кому in await кому_решать(заявка, владельцы):
            try:
                await bot.send_message(кому, текст_заявки(заявка.имя or заявка.логин,
                                                          заявка.логин, р, сейчас, просит),
                                       reply_markup=кнопки_решения(р, заявка.кто, просит),
                                       disable_web_page_preview=True)
                отправлено += 1
            except Exception as беда:  # noqa: BLE001 — один адресат не должен уронить остальных
                logger.warning("заявку в %s не доставить %s: %s", р.путь, кому, беда)
        if not отправлено:
            await callback.answer("некому отправить заявку, напишите владельцу базы",
                                  show_alert=True)
            return
        ждут[ключ] = просит
        _записать_ждут(ждут)
        await callback.answer()
        await callback.message.edit_text(
            f"Заявка на <b>{УРОВНИ[просит]}</b> в {html.escape(р.путь)} отправлена ведущему "
            "работы. Когда он решит, бот напишет.")

    @router.callback_query(F.data.startswith("wa:a:") | F.data.startswith("wa:d:"))
    async def решить(callback: CallbackQuery, bot: Bot) -> None:
        части = (callback.data or "").split(":")
        выдать, ид, кто = части[1] == "a", int(части[2]), int(части[3])
        р = await работа(ид)
        if р is None:
            await callback.answer("такой работы больше нет", show_alert=True)
            return
        if not await право_решать("projects", ид, callback.from_user.id, владельцы):
            await callback.answer("это решает ведущий этой работы", show_alert=True)
            return
        ждут = _ждут()
        try:
            if выдать:
                нужно = int(части[4])
                if await уровень(ид, кто) >= нужно:
                    итог = f"уже было: {УРОВНИ[нужно]} или выше"
                else:
                    await поставить(ид, кто, нужно)
                    итог = f"выдано: {УРОВНИ[нужно]}"
            else:
                итог = "отказано"
        except Exception as беда:  # noqa: BLE001 — показать решающему, а не промолчать
            logger.warning("роль в %s для %s не поставлена: %s", р.путь, кто, беда)
            await callback.answer("GitLab не принял решение, посмотри журнал", show_alert=True)
            return
        ждут.pop(f"{ид}:{кто}", None)
        _записать_ждут(ждут)
        await callback.message.edit_text(
            (callback.message.html_text or "")
            + f"\n\n<b>{итог}</b> · {html.escape(callback.from_user.full_name)}",
            reply_markup=None, disable_web_page_preview=True)
        await callback.answer(итог)
        телеграм = await телеграм_кого(кто)
        if телеграм:
            ответ = (f"В работе {html.escape(р.путь)} {итог}.\n"
                     f'<a href="{html.escape(р.адрес, quote=True)}">Открыть работу</a>'
                     if выдать else
                     f"Заявку в {html.escape(р.путь)} отклонили. Спросите ведущего работы, "
                     "что нужно.")
            try:
                await bot.send_message(телеграм, ответ, disable_web_page_preview=True)
            except Exception as беда:  # noqa: BLE001
                logger.warning("не сказать о решении %s: %s", телеграм, беда)

    @router.message(Command("role"))
    async def роль(message: Message) -> None:
        """`/role <логин> <работа> <reporter|developer|maintainer>` — без заявки."""
        части = (message.text or "").split()
        if len(части) != 4 or части[3].lower() not in ПО_СЛОВУ:
            await message.answer("Формат: /role &lt;логин&gt; &lt;работа&gt; "
                                 "&lt;reporter|developer|maintainer&gt;")
            return
        логин, имя, нужно = части[1].lstrip("@"), части[2].strip("/"), ПО_СЛОВУ[части[3].lower()]
        подходят = [р for р in await все_работы()
                    if р.путь == имя or р.путь.endswith("/" + имя)]
        if len(подходят) != 1:
            await message.answer(f"Работ с именем «{html.escape(имя)}»: {len(подходят)}. "
                                 "Назовите путём: тема/работа.")
            return
        р = подходят[0]
        if not await право_решать("projects", р.ид, message.from_user.id, владельцы):
            await message.answer("Роль в этой работе ставит её ведущий.")
            return
        человек = await пользователь(логин)
        if человек is None:
            await message.answer(f"В базе нет логина <code>{html.escape(логин)}</code>.")
            return
        if await уровень(р.ид, int(человек["id"])) >= нужно:
            await message.answer(f"У {html.escape(логин)} в {html.escape(р.путь)} уже "
                                 f"{УРОВНИ[нужно]} или выше.")
            return
        await поставить(р.ид, int(человек["id"]), нужно)
        await message.answer(f"{html.escape(логин)}: {УРОВНИ[нужно]} в {html.escape(р.путь)}.")
        телеграм = await телеграм_кого(int(человек["id"]), логин)
        if телеграм:
            try:
                await message.bot.send_message(
                    телеграм, f"Вам выдали {УРОВНИ[нужно]} в работе "
                    f'<a href="{html.escape(р.адрес, quote=True)}">{html.escape(р.путь)}</a>.',
                    disable_web_page_preview=True)
            except Exception as беда:  # noqa: BLE001
                logger.warning("не сказать о роли %s: %s", телеграм, беда)

    return router
