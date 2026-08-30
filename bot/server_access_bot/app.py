import asyncio
import html
import logging
from collections.abc import Sequence

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    User,
    WebAppInfo,
)

from .config import Settings
from .domain import Application, ProvisionedAccess
from .provisioning import ProvisioningError, SshProvisioner
from .service import AccessService, ValidationError, validate
from .sheets import GoogleSheetsRepository
from .storage import Storage
from .webapp import WebAppPayloadError, parse_web_app_data

LOGGER = logging.getLogger(__name__)
RULES_URL = "https://vepricov.github.io/ssh_websile"
FORM_URL = f"{RULES_URL}/access-form.html"
VPN_NOTE = (
    "ВАЖНО: перед подключением к H200 получите VPN. Напишите ФИО и "
    "физтеховскую почту Диме @undef1ne5 с просьбой сделать VPN."
)
SERVER_NAMES = {
    "shkodnik_opt": "Старый (mipt)",
    "brain_lab": "Новый (BrainLab)",
    "vv_h200": "H200 (VV)",
}


class Form(StatesGroup):
    confirm = State()


MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(
                text="Открыть форму доступа",
                web_app=WebAppInfo(url=FORM_URL),
            )
        ],
        [KeyboardButton(text="Мои серверы")],
    ],
    resize_keyboard=True,
)


def confirmation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Всё верно, создать доступ", callback_data="form:submit"
                )
            ],
            [InlineKeyboardButton(text="Отмена", callback_data="form:cancel")],
        ]
    )


def application_from_data(user: User, data: dict[str, object]) -> Application:
    return Application(
        telegram_user_id=user.id,
        telegram_username=user.username,
        full_name=str(data["full_name"]),
        server_username=str(data["server_username"]),
        access_term=str(data["access_term"]),
        recommender=str(data["recommender"]),
        activity=str(data.get("activity", "")),
        servers=tuple(str(host) for host in data["servers"]),
    )


def application_data(application: Application) -> dict[str, object]:
    return {
        "full_name": application.full_name,
        "server_username": application.server_username,
        "access_term": application.access_term,
        "recommender": application.recommender,
        "activity": application.activity,
        "servers": list(application.servers),
    }


def application_summary(data: dict[str, object]) -> str:
    selected = [SERVER_NAMES[str(host)] for host in data.get("servers", [])]
    activity = str(data.get("activity", "")) or "не указана"
    return (
        "Проверьте заявку:\n\n"
        f"ФИО: {html.escape(str(data['full_name']))}\n"
        f"Ник на сервере: <code>{html.escape(str(data['server_username']))}</code>\n"
        f"Срок доступа: {html.escape(str(data['access_term']))}\n"
        f"Рекомендатель: {html.escape(str(data['recommender']))}\n"
        f"Серверы: {html.escape(', '.join(selected))}\n"
        f"Деятельность: {html.escape(activity)}"
    )


def credentials_text(accesses: Sequence[ProvisionedAccess]) -> str:
    sections = ["Доступ к вашим серверам:"]
    for access in accesses:
        lines = [SERVER_NAMES[access.host], f"Username: {access.username}"]
        if access.password:
            lines.append(f"Password: {access.password}")
        if access.connection:
            lines.append(f"Подключение: {access.connection}")
        if access.group_url:
            lines.append(f"Telegram-группа: {access.group_url}")
        if access.host == "vv_h200":
            lines.append(VPN_NOTE)
        sections.append("\n".join(lines))
    sections.append(f"Правила и полезная информация: {RULES_URL}")
    return "\n\n".join(sections)


async def delete_later(bot: Bot, chat_id: int, message_id: int, delay: int) -> None:
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramBadRequest:
        pass


def build_router(
    service: AccessService,
    sheets: GoogleSheetsRepository,
    credential_ttl: int,
) -> Router:
    router = Router()

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext) -> None:
        await state.clear()
        if message.chat.type != ChatType.PRIVATE:
            await message.answer(
                "Для получения доступа напишите боту в личные сообщения."
            )
            return
        await message.answer(
            "Я создаю и храню ваши доступы к GPU-серверам. "
            "Откройте форму или посмотрите уже выданные доступы.",
            reply_markup=MAIN_MENU,
        )

    @router.message(F.web_app_data)
    async def web_app_form(message: Message, state: FSMContext) -> None:
        if (
            message.from_user is None
            or message.chat.type != ChatType.PRIVATE
            or message.web_app_data is None
        ):
            return
        try:
            application = parse_web_app_data(
                message.from_user, message.web_app_data.data
            )
            validate(application)
        except (WebAppPayloadError, ValidationError) as error:
            await message.answer(str(error), reply_markup=MAIN_MENU)
            return
        data = application_data(application)
        await state.set_data(data)
        await state.set_state(Form.confirm)
        await message.answer(
            application_summary(data),
            reply_markup=confirmation_keyboard(),
        )

    @router.message(Command("cancel"))
    @router.message(F.text == "Отмена")
    async def cancel_message(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Анкета отменена.", reply_markup=MAIN_MENU)

    @router.callback_query(F.data == "form:cancel")
    async def cancel_callback(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.answer()
        if callback.message:
            await callback.message.edit_text("Анкета отменена.")

    @router.callback_query(Form.confirm, F.data == "form:submit")
    async def submit(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        if callback.message is None:
            return
        await callback.answer("Создаю доступы…")
        await callback.message.edit_reply_markup(reply_markup=None)
        data = await state.get_data()
        try:
            application = application_from_data(callback.from_user, data)
            await asyncio.to_thread(service.submit, application)
            accesses = await asyncio.to_thread(
                service.credentials_for, application.telegram_user_id
            )
        except ValidationError as error:
            await callback.message.answer(str(error), reply_markup=MAIN_MENU)
            return
        except ProvisioningError:
            LOGGER.exception(
                "Provisioning failed for Telegram user %s", callback.from_user.id
            )
            await callback.message.answer(
                "Не удалось создать доступ. Ничего повторно не создавалось. "
                "Попробуйте ещё раз позже.",
                reply_markup=MAIN_MENU,
            )
            return
        status = "active: " + ", ".join(access.host for access in accesses)
        try:
            await asyncio.to_thread(sheets.upsert, application, status)
        except Exception:
            LOGGER.exception("Google Sheets synchronization failed")
        await state.clear()
        secret_message = await callback.message.answer(
            f"<pre>{html.escape(credentials_text(accesses))}</pre>\n\n"
            f"Сообщение удалится через {credential_ttl // 60} мин.",
            protect_content=True,
            reply_markup=MAIN_MENU,
        )
        asyncio.create_task(
            delete_later(
                bot, secret_message.chat.id, secret_message.message_id, credential_ttl
            )
        )

    @router.message(F.text == "Мои серверы")
    async def my_servers(message: Message, bot: Bot) -> None:
        if message.from_user is None or message.chat.type != ChatType.PRIVATE:
            return
        accesses = await asyncio.to_thread(
            service.credentials_for, message.from_user.id
        )
        if not accesses:
            await message.answer(
                "У вас пока нет сохранённых доступов.", reply_markup=MAIN_MENU
            )
            return
        secret_message = await message.answer(
            f"<pre>{html.escape(credentials_text(accesses))}</pre>\n\n"
            f"Сообщение удалится через {credential_ttl // 60} мин.",
            protect_content=True,
            reply_markup=MAIN_MENU,
        )
        asyncio.create_task(
            delete_later(
                bot, secret_message.chat.id, secret_message.message_id, credential_ttl
            )
        )

    return router


async def run(settings: Settings) -> None:
    storage = Storage(settings.database_path, settings.encryption_key)
    service = AccessService(storage, SshProvisioner())
    sheets = GoogleSheetsRepository(
        settings.google_service_account_file,
        settings.spreadsheet_id,
        settings.sheet_id,
    )
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(
        build_router(service, sheets, settings.credential_message_ttl_seconds)
    )
    await dispatcher.start_polling(
        bot, allowed_updates=dispatcher.resolve_used_update_types()
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run(Settings.from_env()))
