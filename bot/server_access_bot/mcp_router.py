import asyncio
import html
import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .mcp_access import McpAccessService

LOGGER = logging.getLogger(__name__)
MCP_MENU = "🧠 База знаний MCP"
ATLAS_URL = "https://brain-lab-research.github.io/claude-brainlab/lab-showcase/#memory"
GUIDE_URL = "https://github.com/brain-lab-research/claude-brainlab/blob/main/docs/mcp-onboarding.md"
MCP_URL = "https://68-183-24-188.sslip.io:9443/mcp"


def actions(request_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Одобрить MCP", callback_data=f"mcp:approve:{request_id}"),
        InlineKeyboardButton(text="Отклонить", callback_data=f"mcp:reject:{request_id}"),
    ]])


def request_text(row: dict) -> str:
    purpose = f"{html.escape(row['purpose'])}\n\n" if row["purpose"] else ""
    return (
        "<b>Заявка в базу знаний MCP</b>\n"
        f"{html.escape(row['full_name'])} · Telegram ID <code>{row['telegram_user_id']}</code>\n\n"
        f"{purpose}"
        "Роль: участник. Чтение общей базы и создание своих проектов. "
        "Запись в существующие проекты — после приглашения их владельцем."
    )


def welcome_text(access: dict, group_url: str = "") -> str:
    text = (
        "<b>Доступ к Lab Knowledge MCP выдан</b>\n\n"
        "Вы можете читать общую базу, создавать свои проекты и работать в них. "
        "Чтобы дополнять существующий проект, попросите его владельца добавить вас "
        "как соавтора (contributor). Соавторы могут изменять и удалять записи проекта.\n\n"
        f"Ваш аккаунт: <code>{html.escape(access['email'])}</code>\n"
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
            await message.answer(request_text(row), reply_markup=actions(row["request_id"]))

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
        await message.answer(
            "<b>Lab Knowledge MCP — общая база исследований</b>\n\n"
            "Подключите её к своему ИИ-агенту: он сможет искать статьи и результаты "
            "лаборатории, читать связанные гипотезы и эксперименты, "
            "сохранять ваши выводы и решения.\n\n"
            f'<a href="{ATLAS_URL}">Посмотреть, как это работает, на сайте Atlas</a> — '
            "сайт открыт всем, данные в нём вымышленные.\n\n"
            "Для доступа к рабочей базе нажмите «Подать заявку». После одобрения "
            "Андреем бот пришлёт личный ключ, инструкцию подключения и группу тестирования.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Подать заявку", callback_data="mcp:request"),
            ]]),
        )

    @router.callback_query(F.data == "mcp:request")
    async def request(callback: CallbackQuery, bot: Bot) -> None:
        if not service.admins:
            await callback.answer("Выдача доступа пока не настроена.", show_alert=True)
            return
        await callback.answer()
        profile = await asyncio.to_thread(service.storage.profile_for, callback.from_user.id)
        try:
            row = await asyncio.to_thread(
                service.request, callback.from_user.id,
                profile.full_name if profile else callback.from_user.full_name,
            )
        except ValueError as error:
            await callback.message.answer(html.escape(str(error)))
            return
        if not row["created"]:
            await callback.message.answer(
                "Ваш доступ к MCP уже активен. Подключение: /mcp."
                if row["status"] in {"granted", "delivered"} else
                "Ваша заявка MCP уже отправлена. После одобрения бот пришлёт подключение."
            )
            return
        notified = False
        for admin_id in service.admins:
            try:
                await bot.send_message(admin_id, request_text(row), reply_markup=actions(row["request_id"]))
                notified = True
            except Exception as error:
                LOGGER.error("MCP request notification failed: %s", type(error).__name__)
        await callback.message.answer(
            "Заявка отправлена. После одобрения подключение придёт сюда."
            if notified else
            "Заявка сохранена, но уведомление Андрею не доставлено. Она доступна ему в /mcp_requests."
        )

    @router.callback_query(F.data.startswith("mcp:approve:"))
    @router.callback_query(F.data.startswith("mcp:reject:"))
    async def decide(callback: CallbackQuery, bot: Bot) -> None:
        if callback.from_user.id not in service.admins:
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        _, action, request_id = callback.data.split(":")
        await callback.answer("Обрабатываю…")
        try:
            if action == "approve":
                access = await asyncio.to_thread(service.approve, callback.from_user.id, request_id)
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
        await callback.message.answer("Подключение MCP доставлено участнику." if action == "approve" else "Заявка отклонена.")

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
