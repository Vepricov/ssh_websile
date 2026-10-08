"""Уведомления из базы знания в телеграм.

**Выключено 02-10-2026 и по умолчанию молчит.** Владелец: «„Предложение ждёт решения“ — я
вот такую хуйню получил раз двадцать, слишком часто и непонятно зачем, про PR мне кажется
можно вообще не присылать».

Жалоба справедлива, и дело не в частоте прохода: письмо уходило по одному на предложение,
как и задумано. Просто предложения стали машинными. 02-10-2026 проходы открыли сорок три
штуки за день — оснастку тридцати четырём работам и записи созвонов восьми, — и каждое
отписалось тому, кто может слить. Уведомление, которое приходит сорок раз за день, ничего не
сообщает: его перестают читать вместе с теми, что важны.

Поэтому почтальон о предложениях молчит. Кто ждёт решения, видно там, где на это и смотрят:
`lab checks` в терминале, список предложений на странице работы, вкладка Merge requests
группы. Красный вердикт автор видит сразу после `lab pr`, в той же команде.

Включить обратно — `LAB_GITLAB_NOTIFY=1`. Код оставлен целиком нарочно: события описаны
верно, и если понадобится звать людей на чужие предложения, это одна переменная, а не
переписывание. Заявки на доступ к базе ходят другим путём (`bot-gitlab_access`) и выключением
не задеты.

Что он умеет, когда включён:

    предложение ждёт решения   → тем, кто может слить: ведущим работы и владельцу
    вердикт покраснел          → автору предложения
    вердикт зелёный            → автору: теперь можно просить слияния
    ответ в обсуждении         → автору, если ответил не он сам

Первый проход **молчит** и только запоминает состояние: иначе при первом запуске люди
получили бы письмо по каждому давно открытому предложению.

Общие части (запросы, карта «логин → телеграм») живут в `gitlab_access`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
from pathlib import Path

from aiogram import Bot

from .gitlab_access import (ПУБЛИЧНЫЙ, api, включено, страницы, телеграм_кого)

logger = logging.getLogger(__name__)

СОСТОЯНИЕ = Path(os.environ.get("LAB_GITLAB_MR_STATE",
                                "/var/lib/server-access-bot/gitlab-mr-state.json"))
#: Кто может слить: ведущий и выше.
СЛИВАЮТ_ОТ = 40
#: Сколько знаков из чужого ответа показывать.
ОТВЕТ_ДО = 400


def _прочитать() -> dict[str, dict]:
    try:
        сырое = json.loads(СОСТОЯНИЕ.read_text(encoding="utf-8"))
        return сырое if isinstance(сырое, dict) else {}
    except (OSError, ValueError):
        return {}


def _записать(состояние: dict[str, dict]) -> None:
    try:
        СОСТОЯНИЕ.parent.mkdir(parents=True, exist_ok=True)
        СОСТОЯНИЕ.write_text(json.dumps(состояние, ensure_ascii=False), encoding="utf-8")
    except OSError as беда:
        logger.warning("не записать состояние предложений: %s", беда)


async def кто_сливает(проект: dict) -> set[int]:
    """Телеграмы тех, кто может слить в этой работе."""
    кому: set[int] = set()
    try:
        члены = await страницы(f"projects/{проект['id']}/members/all")
    except (urllib.error.HTTPError, urllib.error.URLError):
        return кому
    for м in члены:
        if int(м.get("access_level") or 0) < СЛИВАЮТ_ОТ:
            continue
        если = await телеграм_кого(int(м["id"]), str(м.get("username") or ""))
        if если:
            кому.add(если)
    return кому


async def вердикт(проект: dict, sha: str) -> tuple[str, str]:
    """Состояние проверок по коммиту и что они сказали."""
    if not sha:
        return "", ""
    try:
        строки = await страницы(f"projects/{проект['id']}/repository/commits/{sha}/statuses")
    except (urllib.error.HTTPError, urllib.error.URLError):
        return "", ""
    живые = [с for с in строки if с.get("status") not in ("pending", "running")]
    if not живые:
        return "идёт", ""
    плохие = [с for с in живые if с.get("status") != "success"]
    если = плохие[0] if плохие else живые[0]
    состояние = "красный" if плохие else "зелёный"
    return состояние, str(если.get("description") or "")


def имя_работы(проект: dict) -> str:
    return проект.get("name") or проект.get("path") or str(проект.get("id"))


async def разослать(bot: Bot, кому: set[int], текст: str) -> bool:
    ушло = False
    for адрес in кому:
        try:
            await bot.send_message(адрес, текст, disable_web_page_preview=True)
            ушло = True
        except Exception as беда:  # noqa: BLE001 — один адресат не должен ломать рассылку
            logger.warning("уведомление не доставлено %s: %s", адрес, беда)
    return ушло


async def проход(bot: Bot, владельцы: frozenset[int], первый: bool) -> int:
    """Один обход открытых предложений. Возвращает число отправленных писем."""
    состояние = _прочитать()
    отправлено = 0
    живые: set[str] = set()
    проекты = await страницы("projects?archived=false&simple=true")
    for проект in проекты:
        if проект.get("path") in ("literature", "gitlab-profile"):
            continue
        try:
            предложения = await страницы(
                f"projects/{проект['id']}/merge_requests?state=opened")
        except (urllib.error.HTTPError, urllib.error.URLError):
            continue
        for мр in предложения:
            ключ = f"{проект['id']}:{мр['iid']}"
            живые.add(ключ)
            было = состояние.get(ключ) or {}
            автор = мр.get("author") or {}
            тг_автора = await телеграм_кого(int(автор.get("id") or 0),
                                            str(автор.get("username") or ""))
            ссылка = мр.get("web_url") or f"{ПУБЛИЧНЫЙ}/{проект.get('path')}"
            состояние_проверок, что_сказали = await вердикт(проект, str(мр.get("sha") or ""))
            заметки = await страницы(
                f"projects/{проект['id']}/merge_requests/{мр['iid']}/notes?sort=asc")
            свои = [з for з in заметки if not з.get("system")]
            последняя = max((int(з["id"]) for з in свои), default=0)

            if первый:
                # Первый проход только запоминает: иначе люди получат письмо по каждому
                # давно открытому предложению.
                состояние[ключ] = {"сказано": True, "вердикт": состояние_проверок,
                                   "sha": str(мр.get("sha") or ""), "ответ": последняя}
                continue

            # 1. предложение ждёт решения — тем, кто может слить
            if not было.get("сказано"):
                кому = (await кто_сливает(проект)) | set(владельцы)
                кому.discard(тг_автора or 0)
                текст = (f"<b>Предложение ждёт решения</b>\n\n"
                         f"{имя_работы(проект)} !{мр['iid']} — {мр.get('title') or ''}\n"
                         f"от {автор.get('name') or автор.get('username') or '?'}\n"
                         f"проверки: {состояние_проверок or 'ещё не отвечали'}\n"
                         f'<a href="{ссылка}">открыть</a>')
                if await разослать(bot, кому, текст):
                    отправлено += 1

            # 2. вердикт сменился — автору
            if (состояние_проверок and тг_автора
                    and (состояние_проверок != было.get("вердикт")
                         or str(мр.get("sha") or "") != было.get("sha"))
                    and состояние_проверок != "идёт"):
                если_красный = состояние_проверок == "красный"
                текст = (("<b>Проверки по твоему предложению покраснели</b>\n\n"
                          if если_красный else
                          "<b>Проверки по твоему предложению зелёные</b>\n\n")
                         + f"{имя_работы(проект)} !{мр['iid']}\n"
                         + (f"{что_сказали}\n" if если_красный and что_сказали
                            else "Можно просить слияния.\n")
                         + f'<a href="{ссылка}">открыть</a>')
                if await разослать(bot, {тг_автора}, текст):
                    отправлено += 1

            # 3. ответ в обсуждении — автору, если ответил не он
            if тг_автора and последняя > int(было.get("ответ") or 0):
                новые = [з for з in свои if int(з["id"]) > int(было.get("ответ") or 0)
                         and int((з.get("author") or {}).get("id") or 0)
                         != int(автор.get("id") or 0)]
                if новые:
                    з = новые[-1]
                    кто = (з.get("author") or {}).get("name") or "кто-то"
                    текст = (f"<b>Ответ в твоём предложении</b>\n\n"
                             f"{имя_работы(проект)} !{мр['iid']}\n"
                             f"{кто}: {(з.get('body') or '').strip()[:ОТВЕТ_ДО]}\n"
                             f'<a href="{ссылка}">открыть</a>')
                    if await разослать(bot, {тг_автора}, текст):
                        отправлено += 1

            состояние[ключ] = {"сказано": True, "вердикт": состояние_проверок,
                               "sha": str(мр.get("sha") or ""), "ответ": последняя}
    # Закрытые предложения забываются, иначе состояние растёт вечно.
    состояние = {к: v for к, v in состояние.items() if к in живые}
    _записать(состояние)
    return отправлено


async def run_gitlab_notify_loop(bot: Bot, владельцы: frozenset[int],
                                 interval_seconds: int = 180) -> None:
    # Выключено по умолчанию: см. верх файла. Проверка стоит ПЕРЕД первым проходом, чтобы
    # выключенный почтальон не копил состояние и не отправил пачку при включении.
    if os.environ.get("LAB_GITLAB_NOTIFY", "") not in ("1", "да", "yes", "true"):
        logger.info("уведомления о предложениях выключены нарочно "
                    "(включить: LAB_GITLAB_NOTIFY=1)")
        return
    if not включено():
        logger.info("уведомления GitLab выключены: нет LAB_GITLAB_TOKEN")
        return
    первый = not СОСТОЯНИЕ.exists()
    while True:
        try:
            ушло = await проход(bot, владельцы, первый)
            if первый:
                logger.info("первый проход уведомлений: состояние запомнено, писем нет")
            elif ушло:
                logger.info("уведомлений отправлено: %s", ушло)
            первый = False
        except Exception:  # noqa: BLE001 — проход не должен уносить бота
            logger.warning("проход уведомлений не отработал", exc_info=True)
        await asyncio.sleep(interval_seconds)
