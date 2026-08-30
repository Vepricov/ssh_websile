import asyncio
import html
import logging
import secrets
from collections.abc import Sequence
from urllib.parse import urlencode, urlparse

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
from .domain import Application, Profile, ProvisionedAccess, ServerRequest
from .provisioning import ProvisioningError, SshProvisioner
from .service import AccessService, ValidationError, validate, validate_profile
from .sheets import GoogleSheetsRepository
from .storage import Storage
from .webapp import WebAppPayloadError, parse_web_app_data

LOGGER = logging.getLogger(__name__)
RULES_URL = "https://vepricov.github.io/ssh_websile"
FORM_URL = f"{RULES_URL}/access-form.html"
SERVER_FORM_URL = f"{RULES_URL}/server-form.html"
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
                text="Мой профиль",
                web_app=WebAppInfo(url=FORM_URL),
            )
        ],
        [
            KeyboardButton(
                text="Добавиться на сервер",
                web_app=WebAppInfo(url=SERVER_FORM_URL),
            )
        ],
        [KeyboardButton(text="Мои серверы")],
    ],
    resize_keyboard=True,
)


def confirmation_keyboard(request_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Всё верно, создать доступ",
                    callback_data=f"form:submit:{request_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Отмена", callback_data=f"form:cancel:{request_id}"
                )
            ],
        ]
    )


def server_dashboard_keyboard(
    accesses: Sequence[ProvisionedAccess],
) -> InlineKeyboardMarkup | None:
    existing = {access.host for access in accesses}
    rows = [
        [InlineKeyboardButton(text=f"➕ {name}", callback_data=f"servers:add:{host}")]
        for host, name in SERVER_NAMES.items()
        if host not in existing
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def server_request_menu(host: str) -> ReplyKeyboardMarkup:
    if host not in SERVER_NAMES:
        raise ValueError(f"unknown server: {host}")
    url = f"{SERVER_FORM_URL}?{urlencode({'server': host})}"
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text=f"Открыть заявку: {SERVER_NAMES[host]}",
                    web_app=WebAppInfo(url=url),
                )
            ],
            [KeyboardButton(text="Главное меню")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
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


def application_from_request(profile: Profile, request: ServerRequest) -> Application:
    if profile.telegram_user_id != request.telegram_user_id:
        raise ValueError("profile and server request belong to different users")
    return Application(
        telegram_user_id=request.telegram_user_id,
        telegram_username=request.telegram_username,
        full_name=profile.full_name,
        server_username=request.server_username,
        access_term=request.access_term,
        recommender=profile.recommender,
        activity=request.activity,
        servers=request.servers,
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


def server_dashboard_text(accesses: Sequence[ProvisionedAccess]) -> str:
    by_host = {access.host: access for access in accesses}
    sections = ["<b>Мои серверы</b>"]
    for host, name in SERVER_NAMES.items():
        access = by_host.get(host)
        if access is None:
            sections.append(f"○ <b>{html.escape(name)}</b>\nДоступ ещё не добавлен.")
            continue
        lines = [
            f"✅ <b>{html.escape(name)}</b>",
            f"Ник: <code>{html.escape(access.username)}</code>",
        ]
        if access.password:
            lines.append(f"Пароль: <code>{html.escape(access.password)}</code>")
        if access.connection:
            lines.append(f"Подключение: <code>{html.escape(access.connection)}</code>")
        if access.group_url:
            parsed_url = urlparse(access.group_url)
            if (
                parsed_url.scheme == "https"
                and parsed_url.netloc == "t.me"
                and parsed_url.path.strip("/")
                and parsed_url.username is None
                and parsed_url.password is None
            ):
                safe_url = html.escape(access.group_url, quote=True)
                lines.append(f'Telegram-группа: <a href="{safe_url}">открыть</a>')
            else:
                lines.append(f"Telegram-группа: {html.escape(access.group_url)}")
        if access.host == "vv_h200":
            lines.append(html.escape(VPN_NOTE))
        sections.append("\n".join(lines))
    sections.append(
        "☁️ <b>Яндекс-сервер</b>\n"
        'Для получения доступа напишите <a href="https://t.me/aloeme">@aloeme</a>.'
    )
    sections.append(f'Правила: <a href="{RULES_URL}">открыть сайт</a>')
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
            "Сначала заполните профиль, затем выберите сервер. "
            "Все сохранённые доступы находятся в разделе «Мои серверы».",
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
            submission = parse_web_app_data(
                message.from_user, message.web_app_data.data
            )
            if isinstance(submission, Profile):
                validate_profile(submission)
                await asyncio.to_thread(service.storage.save_profile, submission)
                await state.clear()
                await message.answer(
                    "Профиль сохранён. Теперь можно выбрать сервер.",
                    reply_markup=MAIN_MENU,
                )
                return
            profile = await asyncio.to_thread(
                service.storage.profile_for, message.from_user.id
            )
            if profile is None:
                await message.answer(
                    "Сначала заполните «Мой профиль».", reply_markup=MAIN_MENU
                )
                return
            application = application_from_request(profile, submission)
            validate(application)
        except (WebAppPayloadError, ValidationError) as error:
            await message.answer(str(error), reply_markup=MAIN_MENU)
            return
        data = application_data(application)
        request_id = secrets.token_urlsafe(8)
        data["request_id"] = request_id
        await state.set_data(data)
        await state.set_state(Form.confirm)
        await message.answer(
            application_summary(data),
            reply_markup=confirmation_keyboard(request_id),
        )

    @router.message(Command("cancel"))
    @router.message(F.text == "Отмена")
    async def cancel_message(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Анкета отменена.", reply_markup=MAIN_MENU)

    @router.callback_query(F.data.startswith("form:cancel:"))
    async def cancel_callback(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        request_id = (callback.data or "").rsplit(":", 1)[-1]
        if data.get("request_id") != request_id:
            await callback.answer("Эта заявка уже неактуальна.", show_alert=True)
            return
        await state.clear()
        await callback.answer()
        if callback.message:
            await callback.message.edit_text("Анкета отменена.")

    @router.callback_query(Form.confirm, F.data.startswith("form:submit:"))
    async def submit(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        if callback.message is None:
            return
        data = await state.get_data()
        request_id = (callback.data or "").rsplit(":", 1)[-1]
        if data.get("request_id") != request_id:
            await callback.answer("Эта заявка уже неактуальна.", show_alert=True)
            return
        await callback.answer("Создаю доступы…")
        await callback.message.edit_reply_markup(reply_markup=None)
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
            f"{server_dashboard_text(accesses)}\n\n"
            f"Сообщение удалится через {credential_ttl // 60} мин.",
            protect_content=True,
            reply_markup=server_dashboard_keyboard(accesses),
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
        secret_message = await message.answer(
            f"{server_dashboard_text(accesses)}\n\n"
            f"Сообщение удалится через {credential_ttl // 60} мин.",
            protect_content=True,
            reply_markup=server_dashboard_keyboard(accesses),
        )
        asyncio.create_task(
            delete_later(
                bot, secret_message.chat.id, secret_message.message_id, credential_ttl
            )
        )

    @router.callback_query(F.data.startswith("servers:add:"))
    async def add_server(callback: CallbackQuery) -> None:
        host = (callback.data or "").rsplit(":", 1)[-1]
        if host not in SERVER_NAMES or callback.message is None:
            await callback.answer("Неизвестный сервер.", show_alert=True)
            return
        await callback.answer()
        await callback.message.answer(
            f"Откройте короткую заявку на {SERVER_NAMES[host]}.",
            reply_markup=server_request_menu(host),
        )

    @router.message(F.text == "Главное меню")
    async def main_menu(message: Message) -> None:
        await message.answer("Главное меню.", reply_markup=MAIN_MENU)

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
