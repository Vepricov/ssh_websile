import asyncio
import html
import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup, WebAppInfo

from .mcp_access import McpAccessService
from .mcp_profile import FORM_URL, ROLES, is_application, parse_application

LOGGER = logging.getLogger(__name__)
MCP_MENU = "🧠 База знаний MCP"
ATLAS_URL = "https://brain-lab-research.github.io/claude-brainlab/lab-showcase/#memory"
GUIDE_URL = "https://github.com/brain-lab-research/claude-brainlab/blob/main/docs/mcp-onboarding.md"
MCP_URL = "https://68-183-24-188.sslip.io:9443/mcp"


class McpForm(StatesGroup):
    waiting_for_form = State()


FORM_HINT = (
    "Нажмите «Заполнить заявку MCP» на клавиатуре бота и отправьте открывшуюся форму. "
    "Если кнопки скрыты, сверните клавиатуру ввода и раскройте кнопки бота. "
    "Сообщение с анкетой в чате не подаёт заявку."
)


def form_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[
        KeyboardButton(text="Заполнить заявку MCP", web_app=WebAppInfo(url=FORM_URL))],
        [KeyboardButton(text="Главное меню")]], resize_keyboard=True)


def actions(request_id: str, role: str = "member") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"Одобрить · {ROLES[role][0]}", callback_data=f"mcp:grant:{role}:{request_id}"),
    ], [
        InlineKeyboardButton(text="Выбрать другую роль", callback_data=f"mcp:roles:{request_id}"),
        InlineKeyboardButton(text="Отклонить", callback_data=f"mcp:reject:{request_id}"),
    ]])


def request_text(row: dict) -> str:
    role = row.get("approved_role") or row.get("requested_role") or "member"
    contact = f"@{html.escape(row['telegram_username'])}" if row.get("telegram_username") else "Открыть Telegram"
    details = ["<b>Заявка в базу знаний MCP</b>",
               f"<b>{html.escape(row['full_name'])}</b>",
               f"Ник: <b>{html.escape(row.get('username') or row.get('telegram_username') or 'не указан')}</b>",
               f'<a href="tg://user?id={row["telegram_user_id"]}">{contact}</a>',
               f"Рекомендатель: {html.escape(row.get('recommender') or 'не указан')}",
               f"Запрошена роль: <b>{ROLES[row.get('requested_role') or 'member'][0]}</b>",
               ROLES[role][1]]
    if row.get("purpose"):
        details.append(f"Задачи: {html.escape(row['purpose'])}")
    return "\n".join(details)


def welcome_text(access: dict, group_url: str = "") -> str:
    project_access = (
        "Вы можете управлять всеми проектами лаборатории.\n\n"
        if access["role"] == "lead" else
        "Вы можете читать общую базу и создавать свои проекты. "
        "Чтобы дополнять существующий проект, попросите его владельца добавить вас "
        "как соавтора (contributor). Соавторы могут изменять и удалять записи проекта.\n\n"
    )
    text = (
        "<b>Доступ к Lab Knowledge MCP выдан</b>\n\n"
        f"{html.escape(access.get('full_name') or 'Участник')} · <b>{html.escape(access.get('username') or access.get('telegram_username') or 'MCP')}</b>\n"
        f"Роль: <b>{ROLES[access['role']][0]}</b>\n{ROLES[access['role']][1]}\n\n"
        f"{project_access}"
        f"Идентификатор для приглашения в проект: <code>{html.escape(access['email'])}</code>\n"
        f"Адрес MCP: <code>{MCP_URL}</code>\n"
        f"Личный ключ: <tg-spoiler><code>{access['token']}</code></tg-spoiler>\n\n"
        "<b>Как начать</b>\n"
        "1. Установите набор навыков по инструкции ниже.\n"
        "2. В настройках MCP-клиента добавьте этот адрес и заголовок "
        "<code>Authorization: Bearer ВАШ_КЛЮЧ</code>. Для установщика набора: "
        "<code>LAB_MCP_URL</code> и <code>LAB_MCP_TOKEN</code>.\n"
        "3. Попросите агента найти ваш проект и показать его контекст. "
        "Если проекта ещё нет — создайте его через /create-project.\n\n"
        f'<a href="{GUIDE_URL}">Установка и инструкция</a> · '
        f'<a href="{ATLAS_URL}">Atlas: как устроена база</a>\n'
        "Atlas по этой ссылке — демонстрация с учебными данными. "
        "К общей рабочей базе агент подключается по вашему ключу.\n\n"
        "SSH-аккаунт BrainLab не требуется. Ключ не пересылайте. "
        "Повторно получить его или отозвать можно через /mcp."
    )
    if group_url:
        text += f'\n\n<a href="{html.escape(group_url, quote=True)}">Группа тестирования и обратной связи</a>'
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
    for admin_id in service.admins:
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
        await bot.send_message(
            access["telegram_user_id"], welcome_text(access, group_url),
            protect_content=True,
        )
        await asyncio.to_thread(service.delivered, access["request_id"])

    @router.message(Command("mcp_requests"))
    async def pending(message: Message) -> None:
        if message.from_user.id not in service.admins:
            return
        rows = await asyncio.to_thread(service.pending, message.from_user.id)
        if not rows:
            await message.answer("Необработанных заявок MCP нет.")
        for row in rows:
            await message.answer(request_text(row), reply_markup=actions(row["request_id"], row.get("approved_role") or row.get("requested_role") or "member"))

    @router.message(Command("mcp_revoke"))
    async def admin_revoke(message: Message) -> None:
        if message.from_user.id not in service.admins:
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
                ]]),
            )
            return
        if row and row["status"] in {"pending", "approved"}:
            await message.answer("Ваша заявка MCP уже отправлена. После одобрения бот пришлёт подключение.")
            return
        await state.set_state(McpForm.waiting_for_form)
        await message.answer(
            "<b>Lab Knowledge MCP — общая база исследований</b>\n\n"
            "Подключите её к своему ИИ-агенту: он сможет искать статьи и результаты "
            "лаборатории, читать связанные гипотезы и эксперименты, "
            "сохранять ваши выводы и решения.\n\n"
            f'<a href="{ATLAS_URL}">Посмотреть, как это работает, на сайте Atlas</a> — '
            "сайт открыт всем, данные в нём вымышленные.\n\n"
            f"{FORM_HINT}\n\n"
            "В форме укажите ник и выберите роль MCP. ФИО и рекомендатель берутся из "
            "«Мой профиль» в главном меню; заполните профиль, если ещё не сделали этого. После одобрения "
            "Андреем бот пришлёт личный ключ, инструкцию подключения и группу тестирования.",
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
        if callback.from_user.id not in service.admins:
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
        if callback.from_user.id not in service.admins:
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
            LOGGER.error("MCP approval/delivery failed: %s", type(error).__name__)
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
