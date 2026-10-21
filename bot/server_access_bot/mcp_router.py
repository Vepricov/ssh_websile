import asyncio
import html
import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup, WebAppInfo

from .gitlab_access import запомнить_логин
from .mcp_access import McpAccessService
from .mcp_profile import FORM_URL, PREZHNIE, ROLES, is_application, parse_application

LOGGER = logging.getLogger(__name__)
MCP_MENU = "🧠 База знаний MCP"
ATLAS_URL = "https://brain-lab-research.github.io/claude-brainlab/lab-showcase/#memory"
GUIDE_URL = "https://github.com/brain-lab-research/claude-brainlab/blob/main/docs/onboarding.md"
MCP_URL = "https://68-183-24-188.sslip.io:9443/mcp"


class McpForm(StatesGroup):
    waiting_for_form = State()


FORM_HINT = (
    "Нажмите «Заполнить заявку» на клавиатуре бота и отправьте открывшуюся форму. "
    "Если кнопки скрыты, сверните клавиатуру ввода и раскройте кнопки бота. "
    "Набранная в чате анкета заявку не подаёт: нужна именно форма."
)


def form_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[
        KeyboardButton(text="Заполнить заявку", web_app=WebAppInfo(url=FORM_URL))],
        [KeyboardButton(text="Главное меню")]], resize_keyboard=True)


def actions(request_id: str, role: str = "guest") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"Одобрить · {ROLES[role][0]}", callback_data=f"mcp:grant:{role}:{request_id}"),
    ], [
        InlineKeyboardButton(text="Выбрать другую роль", callback_data=f"mcp:roles:{request_id}"),
        InlineKeyboardButton(text="Отклонить", callback_data=f"mcp:reject:{request_id}"),
    ]])


def request_text(row: dict) -> str:
    # Прежние имена ролей переводятся: заявка, поданная до 17-10-2026, иначе уронила бы
    # показ неизвестным ключом.
    role = PREZHNIE.get(сырое := (row.get("approved_role") or row.get("requested_role")
                                  or "guest"), сырое)
    contact = f"@{html.escape(row['telegram_username'])}" if row.get("telegram_username") else "Открыть Telegram"
    details = ["<b>Заявка в базу знаний MCP</b>",
               f"<b>{html.escape(row['full_name'])}</b>",
               f"Ник: <b>{html.escape(row.get('username') or row.get('telegram_username') or 'не указан')}</b>",
               f'<a href="tg://user?id={row["telegram_user_id"]}">{contact}</a>',
               f"Рекомендатель: {html.escape(row.get('recommender') or 'не указан')}",
               f"Запрошена роль: <b>{ROLES[PREZHNIE.get(row.get('requested_role') or 'guest', row.get('requested_role') or 'guest')][0]}</b>",
               ROLES[role][1]]
    if row.get("purpose"):
        details.append(f"Задачи: {html.escape(row['purpose'])}")
    return "\n".join(details)


def welcome_text(access: dict, group_url: str = "") -> str:
    """Что человек читает, когда доступ выдан.

    Порядок здесь не случаен. Прежде сообщение начиналось с MCP и ключа, а вход на сайт
    стоял в конце, причём адрес печатался как текст, а не ссылкой. Человек копировал хост,
    попадал на `/` — то есть на «Your work» в GitLab, список чужих задач, — и не понимал,
    куда пришёл. Владелец 04-10-2026: «вот человек заходит, у него начальное страницу, там
    юворк… это вот, мне кажется, нахуй; там нужно просто ему послать, типа вот, заходи на
    нашу начальную страницу Брайнлаба».

    Поэтому первой строкой идёт ссылка на передний двор лаборатории, а не на корень сайта,
    и дальше три ссылки, отвечающие на вопросы, которые человек задаёт в первый день: что
    тут есть, что есть у меня, как попасть в работу.
    """
    site = (access.get("site_url") or "").rstrip("/")
    login_item = access.get("site_login") or access.get("gitea_login")
    password = access.get("site_password") or access.get("gitea_password")
    name = html.escape(access.get("full_name") or "Участник")
    text = (
        "<b>Доступ в базу знания лаборатории выдан</b>\n\n"
        f"{name} · <b>{html.escape(access.get('username') or access.get('telegram_username') or 'MCP')}</b>\n"
        f"Роль: <b>{ROLES[access['role']][0]}</b>\n{ROLES[access['role']][1]}\n"
    )
    # Группа стоит вторым абзацем, а не последней строкой: в конце после ключей и
    # инструкций её не замечали (20-10-2026).
    if group_url:
        text += (f'\n💬 <b>Общение лаборатории:</b> <a href="{html.escape(group_url, quote=True)}">'
                 "группа BRAIn Lab в Telegram</a> — вопросы, новости и обратная связь. "
                 "Вступайте.\n")
    if site:
        text += (
            f'\n<b>С чего начать:</b> <a href="{html.escape(site, quote=True)}/groups/brainlab">'
            "передняя страница лаборатории</a>\n"
            "Там дерево направлений и тем. У каждой темы своя страница: предмет области, "
            "её работы с утверждениями, кто какую ведёт и кто в команде.\n"
            # Эталон назван прямо в первом сообщении: человек открывает его без заявки, и
            # дальше пишет своё, глядя на него. Все остальные работы закрыты до членства.
            f'\n<b>Эталон:</b> <a href="{html.escape(site, quote=True)}/brainlab/'
            'matrix-optimization/preconditioning-curvature/dykaf">работа dykaf</a> — '
            "единственная, открытая на чтение всем без заявки. Посмотрите, как там "
            "написаны утверждение, прогон и серия, и делайте так же.\n"
        )
    if login_item:
        text += ("\n<b>Вход</b>\n"
                 f"Логин: <code>{html.escape(login_item)}</code>\n")
        if password:
            text += (f"Временный пароль: <tg-spoiler><code>{html.escape(password)}</code>"
                     "</tg-spoiler> — смените при первом входе.\n")
        else:
            text += "Пароль у вас уже есть с прошлого раза.\n"
    if access["role"] == "lead":
        text += "\nВы управляете всеми работами лаборатории.\n"
    elif site:
        text += (
            "\n<b>Что открыто и что по заявке</b>\n"
            "Дерево, темы и вся литература открыты сразу. Внутренности работ — файлы, "
            "предложения, задачи — закрыты до членства: работу видно, содержимое нет.\n"
            "Нужна роль в отдельной работе: кнопка <b>📂 Доступ к работам</b> в меню бота "
            "(или /works). Выберите работу и роль, заявка уйдёт её ведущему, и бот "
            "напишет, когда он решит.\n"
            f'<a href="{html.escape(site, quote=True)}/dashboard/projects">Мои работы</a> · '
            f'<a href="{html.escape(site, quote=True)}/groups/people">Что значит каждая '
            "роль</a>\n"
            "\nПисать в базу напрямую нельзя никому, включая администратора: правка входит "
            "предложением, и слияние человеком и есть запись.\n"
        )
    if access.get("git_token"):
        text += (
            "\n<b>Ключ для git</b>\n"
            "Этим ключом ваш агент клонирует работу и открывает предложения. Скажите ему "
            "один раз:\n"
            f"<pre>export LAB_GIT_URL={html.escape(site)}\n"
            f"export LAB_GIT_LOGIN={html.escape(login_item or '')}\n"
            f"export LAB_GIT_TOKEN={html.escape(access['git_token'])}</pre>\n"
            "Дальше <code>lab here &lt;работа&gt;</code> — и работа склонирована в папку "
            "проекта. Ключ действует год.\n"
        )
    # Про MCP в сообщении больше ничего нет. Ключ по-прежнему выдаётся и лежит в службе,
    # но поиском по базе никто не пользовался, а звать двадцать человек настраивать сервер,
    # который мы собираемся переделывать, — это путать их на ровном месте. Владелец
    # 05-10-2026: «надо удалять, если мы им щас не пользуемся, чтобы не путать меня и
    # агентов; потом настроим, когда прикрутим туда поиск». База читается из клона.
    text += (
        f"\n<b>Приглашение в работу</b>\n"
        f"Чтобы вас позвали в чужую работу, нужен этот идентификатор: "
        f"<code>{html.escape(access['email'])}</code>\n"
        f'\n<a href="{GUIDE_URL}">Установка и инструкция</a> · '
        f'<a href="{ATLAS_URL}">Atlas: как устроена база</a> (демонстрация с учебными '
        "данными)\n"
        "\nКлюч для git не пересылайте. Получить заново или отозвать — /mcp."
    )
    return text


async def submit_application(message: Message, bot: Bot, service: McpAccessService, data: dict) -> None:
    profile = data["profile"]
    row = await asyncio.to_thread(
        service.request, profile.telegram_user_id, profile.full_name, data["activity"],
        username=data["username"], telegram_username=profile.telegram_username or "",
        recommender=profile.recommender, requested_role=data["requested_role"],
    )
    if not row["created"]:
        await message.answer(
            "Ваш доступ к MCP уже активен. Подключение: /mcp."
            if row["status"] in {"granted", "delivered"} else
            "Ваша заявка MCP уже отправлена. Дождитесь решения Андрея."
        )
        return
    notified = False
    for admin_id in service.approvers():
        try:
            await bot.send_message(admin_id, request_text(row), reply_markup=actions(row["request_id"], row["requested_role"]))
            notified = True
        except Exception as error:
            LOGGER.error("MCP request notification failed: %s", type(error).__name__)
    await message.answer(
        f"Заявка отправлена: {html.escape(data['username'])} · {ROLES[data['requested_role']][0]}. После одобрения подключение придёт сюда."
        if notified else
        "Заявка сохранена, но уведомление Андрею не доставлено. Она доступна ему в /mcp_requests."
    )


def build_mcp_router(service: McpAccessService, group_url: str = "") -> Router:
    router = Router(name="mcp_access")
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

    async def deliver(bot: Bot, access: dict) -> None:
        # Связь логина с телеграмом нужна заявкам на доступ: ключ группы чужую почту не
        # видит, а здесь оба известны. Неудача записи не должна ломать выдачу доступа.
        try:
            запомнить_логин(str(access.get("site_login") or ""),
                            int(access.get("telegram_user_id") or 0))
        except Exception as error:  # noqa: BLE001
            LOGGER.warning("карта GitLab не записана: %s", type(error).__name__)
        await bot.send_message(
            access["telegram_user_id"], welcome_text(access, group_url),
            protect_content=True,
        )
        await asyncio.to_thread(service.delivered, access["request_id"])

    @router.message(Command("mcp_requests"))
    async def pending(message: Message) -> None:
        if not service.can_approve(message.from_user.id):
            return
        rows = await asyncio.to_thread(service.pending, message.from_user.id)
        if not rows:
            await message.answer("Необработанных заявок MCP нет.")
        for row in rows:
            await message.answer(request_text(row), reply_markup=actions(row["request_id"], row.get("approved_role") or row.get("requested_role") or "guest"))

    @router.message(Command("mcp_revoke"))
    async def admin_revoke(message: Message) -> None:
        if not service.can_approve(message.from_user.id):
            return
        parts = (message.text or "").split()
        if len(parts) != 2 or not parts[1].isdigit():
            await message.answer("Формат: /mcp_revoke TELEGRAM_ID")
            return
        try:
            await asyncio.to_thread(service.revoke, message.from_user.id, int(parts[1]))
        except ValueError as error:
            await message.answer(str(error))
        except Exception as error:
            LOGGER.error("MCP revocation failed: %s", type(error).__name__)
            await message.answer("Отзыв не подтверждён. Повторите команду.")
        else:
            await message.answer("Ключ MCP отозван.")

    @router.message(Command("mcp"))
    @router.message(F.text == MCP_MENU)
    async def start(message: Message, state: FSMContext) -> None:
        await state.clear()
        if not service.admins:
            await message.answer("Выдача доступа к MCP пока не настроена.")
            return
        row = await asyncio.to_thread(service.get, message.from_user.id)
        if row and row["status"] in {"granted", "delivered"}:
            await message.answer(
                "<b>Ваш доступ к MCP активен</b>\n"
                "Получить ключ повторно можно только здесь, в личном чате.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="Показать подключение", callback_data="mcp:show"),
                    InlineKeyboardButton(text="Отозвать ключ", callback_data="mcp:revoke"),
                ], [
                    InlineKeyboardButton(text="📂 Доступ к работам", callback_data="wa:start"),
                ]]),
            )
            return
        if row and row["status"] in {"pending", "approved"}:
            await message.answer("Ваша заявка MCP уже отправлена. После одобрения бот пришлёт подключение.")
            return
        await state.set_state(McpForm.waiting_for_form)
        await message.answer(
            "<b>База знания лаборатории</b>\n\n"
            "Общая память: у каждой работы свой репозиторий, внутри — её утверждения, "
            "прогоны и числа, рядом разборы статей темы. Чтобы на вопрос «это кто-нибудь "
            "уже пробовал» был ответ, а у числа в статье — адрес, по которому его можно "
            "открыть и проверить.\n\n"
            "<b>Это git, и работают с ней как с репозиторием.</b> Дерево тем и вся "
            "литература открыты вам сразу; работы закрыты, и роль в работе даётся по "
            "заявке кнопкой «📂 Доступ к работам» — решает её ведущий. Писать в базу напрямую нельзя никому, включая "
            "администратора: правка входит предложением о слиянии, и слияние человеком "
            "и есть запись.\n\n"
            "После одобрения бот пришлёт всё сразу: вход на сайт базы, ключ для git — им "
            "агент клонирует работу и открывает предложения, — и ключ MCP, которым агент "
            "ищет по базе, не открывая браузер.\n\n"
            f'<a href="{ATLAS_URL}">Посмотреть, как это устроено</a> — витрина открыта '
            "всем, данные в ней учебные.\n\n"
            f"{FORM_HINT}\n\n"
            "В форме укажите имя, ник и роль. Ник станет логином на сайте, поэтому "
            "латиницей. SSH-аккаунт к серверам для базы не нужен: это разные доступы.",
            reply_markup=form_menu(),
        )

    @router.callback_query(F.data == "mcp:request")
    async def old_request(callback: CallbackQuery) -> None:
        await callback.answer()
        await callback.message.answer(FORM_HINT, reply_markup=form_menu())

    @router.message(McpForm.waiting_for_form, F.text.contains("\n"), ~F.text.startswith("/"))
    async def typed_application(message: Message) -> None:
        await message.answer(
            f"Заявка ещё не отправлена.\n\n{FORM_HINT}", reply_markup=form_menu(),
        )

    @router.message(F.web_app_data.data.func(is_application))
    async def request(message: Message, bot: Bot, state: FSMContext) -> None:
        if not service.admins:
            await message.answer("Выдача доступа пока не настроена.")
            return
        try:
            data = parse_application(message.from_user, message.web_app_data.data)
            profile = data["profile"]
        except ValueError as error:
            await message.answer(html.escape(str(error)), reply_markup=form_menu())
            return
        await asyncio.to_thread(service.storage.save_profile, profile)
        await state.clear()
        await submit_application(message, bot, service, data)

    @router.callback_query(F.data.startswith("mcp:roles:"))
    async def choose_role(callback: CallbackQuery) -> None:
        if not service.can_approve(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        await callback.answer()
        request_id = callback.data.split(":")[-1]
        await callback.message.reply(
            "<b>Какую роль одобрить для этой заявки?</b>\n\n" + "\n\n".join(f"<b>{name}</b>\n{description}" for name, description in ROLES.values()),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=f"Одобрить · {name}", callback_data=f"mcp:grant:{role}:{request_id}")
            ] for role, (name, _) in ROLES.items()]),
        )

    @router.callback_query(F.data.startswith("mcp:grant:"))
    @router.callback_query(F.data.startswith("mcp:approve:"))
    @router.callback_query(F.data.startswith("mcp:reject:"))
    async def decide(callback: CallbackQuery, bot: Bot) -> None:
        if not service.can_approve(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        parts = callback.data.split(":")
        action, request_id = parts[1], parts[-1]
        role = parts[2] if action == "grant" and len(parts) == 4 else None
        await callback.answer("Обрабатываю…")
        try:
            if action in {"approve", "grant"}:
                access = await asyncio.to_thread(service.approve, callback.from_user.id, request_id, role)
                await deliver(bot, access)
            else:
                user_id = await asyncio.to_thread(service.reject, callback.from_user.id, request_id)
                await bot.send_message(user_id, "Заявка MCP отклонена. Уточните детали у Андрея.")
        except ValueError as error:
            await callback.message.answer(html.escape(str(error)))
            return
        except Exception as error:
            # Имени исключения мало: 04-10-2026 две заявки встали, в журнале лежал
            # голый `RuntimeError`, и причину пришлось искать запуском скрипта выдачи
            # руками. Мост теперь дописывает в сообщение строку stderr дочернего
            # скрипта — там только имя ошибки и её текст, без ключей.
            LOGGER.exception("MCP approval/delivery failed: %s: %s",
                             type(error).__name__, error)
            await callback.message.answer(
                "Выдача или доставка не завершена. Нажмите «Одобрить MCP» повторно: "
                "аккаунт и ключ не дублируются. Заявка сохранена в /mcp_requests."
            )
            return
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(f"Подключение доставлено: {html.escape(access.get('full_name', ''))} · {ROLES[access['role']][0]}." if action in {"approve", "grant"} else "Заявка отклонена.")

    @router.callback_query(F.data == "mcp:show")
    async def show(callback: CallbackQuery, bot: Bot) -> None:
        row = await asyncio.to_thread(service.get, callback.from_user.id)
        if not row or row["status"] not in {"granted", "delivered"}:
            await callback.answer("Нет активного подключения.", show_alert=True)
            return
        await callback.answer()
        # The saved approver is used only for redelivery of an already issued key.
        access = await asyncio.to_thread(service.approve, row["approved_by"], row["request_id"])
        await deliver(bot, access)

    @router.callback_query(F.data == "mcp:revoke")
    async def confirm_revoke(callback: CallbackQuery) -> None:
        await callback.answer()
        await callback.message.answer(
            "Агенты с этим ключом потеряют доступ к MCP. Отозвать его?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Да, отозвать мой ключ", callback_data="mcp:revoke-confirm"),
            ]]),
        )

    @router.callback_query(F.data == "mcp:revoke-confirm")
    async def revoke(callback: CallbackQuery) -> None:
        await callback.answer()
        try:
            await asyncio.to_thread(service.revoke, callback.from_user.id, callback.from_user.id)
        except ValueError as error:
            await callback.message.answer(str(error))
        except Exception as error:
            LOGGER.error("MCP self-revocation failed: %s", type(error).__name__)
            await callback.message.answer("Отзыв не подтверждён. Повторите попытку.")
        else:
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer("Ключ отозван. Новая заявка: /mcp.")

    return router
