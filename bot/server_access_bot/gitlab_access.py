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
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery

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
#: Корень базы и уровень, который делает человека лидом лаборатории. Определение принято
#: владельцем 17-10-2026: «пусть все лиды будут автоматически ментейнерами в корне, и это
#: будет определение лида». Отсюда же берутся те, кому звонить про нового человека.
КОРЕНЬ = os.environ.get("LAB_GITLAB_ROOT", "brainlab")
ЛИД = ВЕДУЩИЙ
#: Лиды нужны и синхронному коду (кнопки заявки в лабораторию), а опрос GitLab идёт в
#: фоновом проходе. Поэтому список лежит файлом: проход его обновляет, остальные читают.
ЛИДЫ = Path(os.environ.get("LAB_GITLAB_LEADS",
                           "/var/lib/server-access-bot/gitlab-lidy.json"))
НАЗВАНИЕ_УРОВНЯ = {ЧИТАТЕЛЬ: "читатель", РАЗРАБОТЧИК: "участник", ВЕДУЩИЙ: "ведущий"}
#: Кто имеет право решать — зависит от того, куда постучались, и это не наше правило, а
#: устройство GitLab. Замер 17-10-2026 ключом ведущего: `groups/12/access_requests` отвечает
#: 403, `projects/<работа>/access_requests` открывается. Значит состав ГРУППЫ (тема,
#: направление) ведёт только владелец, а состав РАБОТЫ — ведущий.
#:
#: Слать заявку тому, кто не может её решить, хуже, чем не слать вовсе: человек открывает
#: страницу и не находит кнопки.
ВЛАДЕЛЕЦ = 50
#: Уведомление идёт ВЛАДЕЛЬЦАМ места, а не всем, кто вправе одобрить. Владелец 17-10-2026:
#: «уведомление того, что кто-то хочет постучаться в мой проект, должно приходить только
#: оунерам». Лиды лаборатории — ведущие в каждой работе по наследству, и звонить им всем
#: значит вернуть прежнее «все заявки одному».
РЕШАЮТ_ОТ = {"groups": ВЛАДЕЛЕЦ, "projects": ВЛАДЕЛЕЦ}


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
    корень: bool = False

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
_места: list[tuple[str, int, str, str, bool]] = []
_места_до = 0.0
МЕСТА_ЖИВУТ = 600


async def места_заявок(сейчас: float) -> list[tuple[str, int, str, str, bool]]:
    global _места, _места_до
    if _места and сейчас < _места_до:
        return _места
    собрано: list[tuple[str, int, str, str, bool]] = []
    for г in await страницы("groups"):
        собрано.append(("groups", г["id"], г.get("name") or г["full_path"],
                        f"{ПУБЛИЧНЫЙ}/groups/{г['full_path']}/-/group_members"
                        f"?tab=access_requests",
                        "/" not in str(г.get("full_path") or "")))
    for п in await страницы("projects?archived=false"):
        if п.get("path") in ОТКРЫТОЕ:
            continue
        собрано.append(("projects", п["id"], п.get("name") or п["path_with_namespace"],
                        f"{ПУБЛИЧНЫЙ}/{п['path_with_namespace']}/-/project_members"
                        f"?tab=access_requests", False))
    _места, _места_до = собрано, сейчас + МЕСТА_ЖИВУТ
    return собрано


async def собрать_заявки() -> list[Заявка]:
    """Все ждущие заявки: и на группы, и на проекты."""
    места = await места_заявок(asyncio.get_running_loop().time())
    вышло: list[Заявка] = []
    for вид, ид, имя_места, адрес, корень in места:
        try:
            строки = await страницы(f"{вид}/{ид}/access_requests")
        except (urllib.error.HTTPError, urllib.error.URLError):
            continue
        for с in строки:
            логин = str(с.get("username") or "")
            вышло.append(Заявка(
                вид=вид, ид=ид, путь=имя_места, адрес=адрес, кто=int(с["id"]),
                логин=логин, имя=str(с.get("name") or ""),
                телеграм=await телеграм_кого(int(с["id"]), логин), корень=корень))
    return вышло


#: Строка README бывает общей с другими полями («Ведёт … · **Тема** …»), поэтому
#: берётся только то, что стоит ДО следующего поля: иначе ссылка на тему сошла бы
#: за логин ведущего.
ВЕДУТ = re.compile(r"\*\*Вед[уё]т\*\*\s*((?:(?!\*\*).)+)")
ЧЕЛОВЕК = re.compile(r"\[([^\]]+)\]\(([^)]*?/)?([A-Za-z0-9._-]+)\)")


async def главные_из_readme(ид: int) -> set[int]:
    """Ведущие работы — из строки «Ведут» её README, а не из прав.

    Владелец 17-10-2026: «оунеры, ну, короче, главные в теме, в проекте, в лаборатории. их
    нужно, получается, брать из ридмишки и уведомления им в боте».

    Право и уведомление — разные вещи: одобрить может любой `Maintainer` работы, даже если
    ему не писали. Мы решаем только, КОМУ ПОЗВОНИТЬ, и звоним тому, кто назван главным в
    самой работе. Если строки нет, остаётся прежний способ — по членству.
    """
    try:
        сырое = await текст_файла(ид, "README.md")
    except (urllib.error.HTTPError, urllib.error.URLError):
        return set()
    строка = ВЕДУТ.search(сырое[:6000])
    if not строка:
        return set()
    вышло: set[int] = set()
    for _, _, логин in ЧЕЛОВЕК.findall(строка.group(1)):
        люди = await страницы(f"users?username={логин}")
        if люди:
            кто = await телеграм_кого(int(люди[0]["id"]), логин)
            if кто:
                вышло.add(кто)
    return вышло


async def текст_файла(ид: int, путь: str) -> str:
    адрес = (f"{АДРЕС}/api/v4/projects/{ид}/repository/files/"
             f"{urllib.parse.quote(путь, safe='')}/raw?ref=HEAD")
    запрос = urllib.request.Request(
        адрес, headers={"PRIVATE-TOKEN": os.environ.get("LAB_GITLAB_TOKEN", "")})
    чтение = await asyncio.get_running_loop().run_in_executor(
        None, lambda: urllib.request.urlopen(запрос, timeout=30).read())
    return чтение.decode("utf-8", "replace")


async def кому_решать(заявка: Заявка, владельцы: frozenset[int]) -> set[int]:
    """Главным ТОГО места, куда постучались. Выше поднимаемся, только если там никого.

    Владелец 17-10-2026: «доступ в саму базу выдаю только я (ну можно сделать чтобы все
    лиды выдавали). потом если он просится в тему то приходит уведомление всем главным по
    этой теме. если в подтему то в подтеме главным. и если в работу то главным по работе».

    До этого собирались все уровни разом, и лиды темы получали заявки в каждую её работу,
    хотя у работы есть свой ведущий. Теперь спрашивается сначала само место, и только если
    у него главных нет, заявка поднимается к подтеме, затем к теме: иначе она повисла бы.

    Уровень, дающий право решать, разный: состав ГРУППЫ ведёт владелец, состав РАБОТЫ —
    ведущий (проверено: ведущему `groups/<id>/access_requests` отвечает 403).

    Корень — это вход в саму базу, и его заявки идут владельцам лаборатории, включая
    самого владельца базы: здесь он не исключается, потому что это его решение.
    """
    свои = set()
    if заявка.вид == "projects":
        свои = await главные_из_readme(заявка.ид)
    свои = свои or await главные(заявка.вид, заявка.ид, РЕШАЮТ_ОТ[заявка.вид])
    if заявка.вид == "groups" and await это_корень(заявка.ид):
        # Вход в саму лабораторию решают владелец и все лиды: это новый человек, а не
        # доступ к чьей-то работе, и ждать одного человека тут незачем.
        return свои | set(владельцы) | await лиды_лаборатории()
    адресаты = свои - set(владельцы)
    if адресаты:
        return адресаты
    for группа in await родители(заявка):
        выше = await главные("groups", группа, ВЛАДЕЛЕЦ) - set(владельцы)
        if выше:
            return выше
    return set(владельцы)


async def главные(вид: str, ид: int, порог: int) -> set[int]:
    """Те, кто вправе решать В ЭТОМ месте: записанные на нём, без наследованных.

    Наследованные — лиды лаборатории: они состоят везде по должности, и считать их
    главными каждой работы значит снова свести все заявки к ним.
    """
    вышло: set[int] = set()
    try:
        члены = await страницы(f"{вид}/{ид}/members")
    except (urllib.error.HTTPError, urllib.error.URLError):
        return вышло
    for м in члены:
        if int(м.get("access_level") or 0) < порог:
            continue
        кто = await телеграм_кого(int(м["id"]), str(м.get("username") or ""))
        if кто:
            вышло.add(кто)
    return вышло


async def лиды_лаборатории() -> set[int]:
    """Телеграмы лидов: Maintainer и выше в корне базы.

    Владелец 18-10-2026: «я хочу, чтобы нового участника одобряли все мантейнеры. То есть
    все лиды могли в боте одобрить вступление нового участника, и они там могли выбрать
    ему роль. Чтобы им тоже сообщение приходило».

    Своим ключом лид заявку в корень решить не может (`groups/<корень>/access_requests`
    отвечает ему 403: состав группы ведёт владелец), и это не обходится настройкой. Зато
    бот ходит в GitLab своим ключом, поэтому решение лида он исполняет сам — проверив
    здесь, что нажавший действительно лид.
    """
    вышло: set[int] = set()
    try:
        члены = await страницы(f"groups/{urllib.parse.quote(КОРЕНЬ, safe='')}/members")
    except (urllib.error.HTTPError, urllib.error.URLError):
        return лиды_из_кеша()
    for м in члены:
        логин = str(м.get("username") or "")
        if int(м.get("access_level") or 0) < ЛИД:
            continue
        if логин in ("root",) or логин.endswith("-agent") or логин.startswith("group_"):
            continue
        кто = await телеграм_кого(int(м["id"]), логин)
        if кто:
            вышло.add(кто)
    _записать_лидов(вышло)
    return вышло


def лиды_из_кеша() -> frozenset[int]:
    """То же, но без похода в GitLab: кнопку нажимают не в проходе."""
    try:
        return frozenset(int(x) for x in json.loads(ЛИДЫ.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return frozenset()


def _записать_лидов(кто: set[int]) -> None:
    try:
        ЛИДЫ.parent.mkdir(parents=True, exist_ok=True)
        ЛИДЫ.write_text(json.dumps(sorted(кто)), encoding="utf-8")
    except OSError as беда:
        logger.warning("не записать лидов лаборатории: %s", беда)


async def это_корень(ид: int) -> bool:
    try:
        группа = await api(f"groups/{ид}")
    except (urllib.error.HTTPError, urllib.error.URLError):
        return False
    return "/" not in str(группа.get("full_path") or "")


async def родители(заявка: Заявка) -> list[int]:
    """Номера групп над местом заявки: подтема и тема. Снизу вверх, корень не берём.

    Корень — это вся лаборатория, и его владельцы и так стоят запасным вариантом.
    """
    try:
        место = await api(f"{заявка.вид}/{заявка.ид}")
    except (urllib.error.HTTPError, urllib.error.URLError):
        return []
    номер = (место.get("namespace") or {}).get("id") if заявка.вид == "projects" \
        else место.get("parent_id")
    вышло: list[int] = []
    while номер and len(вышло) < 3:
        try:
            группа = await api(f"groups/{номер}")
        except (urllib.error.HTTPError, urllib.error.URLError):
            break
        # Корень (`brainlab`, `ops`) пропускаем: его владельцы — лиды лаборатории, они и
        # так стоят запасным вариантом. Иначе каждая заявка в каждую работу приходила бы
        # им, а это ровно то, от чего уходили.
        if "/" not in str(группа.get("full_path") or ""):
            break
        вышло.append(int(номер))
        номер = группа.get("parent_id")
    return вышло


#: Роли названы ТАК ЖЕ, КАК В GITLAB, потому что выбирают их там — в выпадающем списке на
#: странице состава, где написано `Owner`, `Maintainer`, `Developer`, `Reporter`, `Guest`.
#: Владелец 17-10-2026: «вот тех ролей которые ты описал их же блять не существует сука.
#: роли они выдаются блять в гитлабе и выглядят вот так». До этого здесь стояли наши
#: переводы, и человек искал в списке слова, которых там нет.
РОЛИ_РАБОТЫ = (
    "<b>Reporter</b> — видит работу и её утверждения, писать не может.",
    "<b>Developer</b> — пушит ветки и открывает предложения, сливать не может. "
    "Обычный ответ тому, кто идёт работать.",
    "<b>Maintainer</b> — то же плюс слияние предложений и право принимать в работу.",
)
РОЛИ_ТЕМЫ = (
    "<b>Reporter</b> — видит все работы темы и её литературу, писать не может. "
    "Обычный доступ студента.",
    "<b>Developer</b> — пушит ветки во всех работах темы и заводит в ней новые работы.",
    "<b>Maintainer</b> — то же плюс слияние предложений во всех работах темы.",
    "<b>Owner</b> — то же плюс право вести состав: принимать в тему и решать заявки в её "
    "работы. Это и есть главный по теме.",
)


#: Вход в саму лабораторию: четыре ступени, принятые владельцем 17-10-2026. Это та же
#: лестница, что в книжке базы и в форме бота, и названия тоже гитлабовские.
РОЛИ_КОРНЯ = (
    "<b>Guest</b> — видит дерево направлений, тем и работ и всю литературу. Содержимое "
    "работ закрыто. С этого начинают все.",
    "<b>Reporter</b> — читает работы целиком: утверждения, прогоны, рукописи, "
    "обсуждения. Писать нельзя, обсуждать можно. Обычный доступ студента.",
    "<b>Developer</b> — то же плюс работа: ветки и предложения в любой работе, свои "
    "новые работы в любой теме. Сливать не может.",
    "<b>Maintainer</b> — лид лаборатории: сливает предложения везде, заводит темы и "
    "владеет теми, где ведёт работу. Выдаёт её только владелец базы.",
)


def текст_заявки(заявка: Заявка) -> str:
    """Заявка решается НА СТРАНИЦЕ GitLab, а не кнопкой в телеграме.

    Владелец 17-10-2026: «надо описать все те [роли] которые на гитлабе будут выданы и
    подписать что они значат». Список разный у работы и у группы: в группе есть владелец,
    и это главный по теме, а в работе такого уровня не дают — состав работы ведёт ведущий.
    """
    кто = заявка.имя or заявка.логин
    это_работа = заявка.вид == "projects"
    куда = ("просится в работу" if это_работа else
            "просится в лабораторию" if заявка.корень else "просит доступ к теме")
    роли = (РОЛИ_КОРНЯ if заявка.корень else
            РОЛИ_РАБОТЫ if это_работа else РОЛИ_ТЕМЫ)
    return (f"<b>Заявка на доступ в базу знания</b>\n\n"
            f"{кто} (<code>{заявка.логин}</code>) {куда} <b>{заявка.путь}</b>\n\n"
            f'<a href="{заявка.адрес}">Решить в GitLab</a> — там же видно, '
            f"кто уже есть.\n\n"
            f"Роль выбираешь ты при одобрении: заявка её не несёт. В GitLab роли "
            f"называются так же, как здесь.\n"
            + "\n".join(f"• {строка}" for строка in роли)
            + "\n\nУровень в корне — пол для всего дерева: ниже него человек не "
              "опустится нигде, а выше поднять можно в любой теме и работе.")


async def run_gitlab_access_loop(bot: Bot, владельцы: frozenset[int],
                                 interval_seconds: int = 180) -> None:
    """Опрашивать GitLab и присылать новые заявки тем, кто их решает."""
    if not включено():
        logger.info("заявки GitLab выключены: нет LAB_GITLAB_TOKEN")
        return
    известные = _прочитать_состояние()
    while True:
        try:
            # Список лидов обновляется здесь же: кнопкам он нужен без похода в GitLab.
            await лиды_лаборатории()
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
                                               disable_web_page_preview=True)
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
    """Разбор нажатий на кнопках СТАРЫХ сообщений: новые заявки приходят без них.

    Кнопки сняты 17-10-2026, но разосланные до этого сообщения висят в переписке, и нажатие
    на них не должно упираться в тишину.
    """
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
    # В корень принимает любой лид, в тему и работу — главные этого места.
    порог = РЕШАЮТ_ОТ[вид]
    if вид == "groups" and await это_корень(ид):
        if телеграм in лиды_из_кеша():
            return True
        порог = ЛИД
    try:
        члены = await страницы(f"{вид}/{ид}/members/all")
    except (urllib.error.HTTPError, urllib.error.URLError):
        return False
    for м in члены:
        if int(м.get("access_level") or 0) < порог:
            continue
        if await телеграм_кого(int(м["id"]), str(м.get("username") or "")) == телеграм:
            return True
    return False
