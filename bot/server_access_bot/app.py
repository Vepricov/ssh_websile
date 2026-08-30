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
    BotCommand,
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
from .domain import (
    Application,
    Profile,
    ProvisionedAccess,
    RegistryMatch,
    ServerRequest,
)
from .provisioning import ProvisioningError, SshProvisioner
from .service import AccessService, ValidationError, validate, validate_profile
from .sheets import (
    GoogleSheetsRepository,
    RegistryAmbiguousError,
    normalize_telegram_username,
)
from .storage import RegistryAlreadyClaimedError, Storage
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
ALL_SERVICE_NAMES = {**SERVER_NAMES, "yandex": "Яндекс-сервер"}


class Form(StatesGroup):
    confirm = State()
    registry_confirm = State()
    edit_full_name = State()
    edit_recommender = State()


MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="👤 Мой профиль"),
            KeyboardButton(text="🔐 Мои серверы"),
        ],
        [
            KeyboardButton(text="🖥 Доступные серверы"),
            KeyboardButton(
                text="➕ Получить доступ", web_app=WebAppInfo(url=SERVER_FORM_URL)
            ),
            KeyboardButton(text="📚 Сайт и правила"),
        ],
    ],
    resize_keyboard=True,
)


def profile_form_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="Открыть форму профиля",
                    web_app=WebAppInfo(url=FORM_URL),
                )
            ],
            [KeyboardButton(text="Главное меню")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def profile_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Изменить ФИО", callback_data="profile:edit:full_name"
                ),
                InlineKeyboardButton(
                    text="Изменить рекомендателя",
                    callback_data="profile:edit:recommender",
                ),
            ]
        ]
    )


def registry_confirmation_keyboard(link_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, это я", callback_data=f"registry:confirm:{link_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Нет, это не я", callback_data=f"registry:reject:{link_id}"
                )
            ],
        ]
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
    registered_services: Sequence[str] = (),
) -> InlineKeyboardMarkup | None:
    existing = {access.host for access in accesses} | set(registered_services)
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


def registry_match_data(match: RegistryMatch) -> dict[str, object]:
    return {
        "telegram_username": match.telegram_username,
        "full_name": match.full_name,
        "server_username": match.server_username,
        "access_term": match.access_term,
        "recommender": match.recommender,
        "activity": match.activity,
        "servers": list(match.servers),
        "source_row_count": match.source_row_count,
        "telegram_user_id": match.telegram_user_id,
    }


def registry_match_from_data(data: dict[str, object]) -> RegistryMatch:
    return RegistryMatch(
        telegram_username=str(data["telegram_username"]),
        full_name=str(data["full_name"]),
        server_username=str(data["server_username"]),
        access_term=str(data["access_term"]),
        recommender=str(data["recommender"]),
        activity=str(data.get("activity", "")),
        servers=tuple(str(host) for host in data["servers"]),
        source_row_count=int(data["source_row_count"]),
        telegram_user_id=(
            int(data["telegram_user_id"])
            if data.get("telegram_user_id") is not None
            else None
        ),
    )


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


def available_servers_text() -> str:
    return (
        "<b>Доступные серверы</b>\n\n"
        "<b>Старый (mipt)</b>\n"
        "4 × RTX 2080 11 GB, 2 × A100 40 GB, 2 × A100 80 GB\n\n"
        "<b>Новый (BrainLab)</b>\n"
        "4 × A100 80 GB\n\n"
        "<b>H200 (VV)</b>\n"
        "8 × H200 140 GB, требуется VPN\n\n"
        "<b>Яндекс-сервер</b>\n"
        'Для получения доступа напишите <a href="https://t.me/aloeme">@aloeme</a>.'
    )


def registry_confirmation_text(match: RegistryMatch) -> str:
    services = ", ".join(ALL_SERVICE_NAMES[host] for host in match.servers)
    return (
        "Я нашёл ваши данные в существующей таблице. Это вы?\n\n"
        f"ФИО: {html.escape(match.full_name)}\n"
        f"Ник на серверах: <code>{html.escape(match.server_username)}</code>\n"
        f"Сервисы: {html.escape(services or 'не указаны')}"
    )


def registry_link_is_trusted(match: RegistryMatch, telegram_user_id: int) -> bool:
    return match.telegram_user_id == telegram_user_id


def my_info_text(
    profile: Profile,
    registry: RegistryMatch | None,
    accesses: Sequence[ProvisionedAccess],
) -> str:
    access_hosts = {access.host for access in accesses}
    registered_hosts = set(registry.servers if registry else ())
    connected = access_hosts | registered_hosts
    connected_names = [
        ALL_SERVICE_NAMES[host] for host in ALL_SERVICE_NAMES if host in connected
    ]
    server_username = registry.server_username if registry else None
    if server_username is None and accesses:
        server_username = accesses[0].username
    telegram_username = (
        f"@{html.escape(profile.telegram_username)}"
        if profile.telegram_username
        else "не указан"
    )
    return (
        "<b>Мой профиль</b>\n\n"
        f"ФИО: {html.escape(profile.full_name)}\n"
        f"Telegram: {telegram_username}\n"
        f"Рекомендатель: {html.escape(profile.recommender)}\n"
        + (
            f"Ник на серверах: <code>{html.escape(server_username)}</code>\n"
            if server_username
            else "Ник на серверах: не указан\n"
        )
        + "Подключённые сервисы: "
        + html.escape(", ".join(connected_names) or "нет")
    )


def server_dashboard_text(
    accesses: Sequence[ProvisionedAccess],
    registry: RegistryMatch | None = None,
) -> str:
    by_host = {access.host: access for access in accesses}
    registered = set(registry.servers if registry else ())
    sections = ["<b>Мои серверы</b>"]
    for host, name in SERVER_NAMES.items():
        access = by_host.get(host)
        if access is None:
            if host in registered and registry is not None:
                sections.append(
                    f"✅ <b>{html.escape(name)}</b>\n"
                    f"Ник: <code>{html.escape(registry.server_username)}</code>\n"
                    "Доступ найден в таблице.\n"
                    "Пароль не сохранён ботом."
                )
                continue
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

    async def answer_profile(message: Message, profile: Profile) -> None:
        registry, accesses = await asyncio.gather(
            asyncio.to_thread(
                service.storage.registry_match_for, profile.telegram_user_id
            ),
            asyncio.to_thread(service.credentials_for, profile.telegram_user_id),
        )
        await message.answer(
            my_info_text(profile, registry, accesses),
            reply_markup=profile_keyboard(),
        )

    async def offer_registry_link(message: Message, state: FSMContext) -> bool:
        if message.from_user is None or not message.from_user.username:
            return False
        try:
            match = await asyncio.to_thread(
                sheets.find_registry_user, message.from_user.username
            )
        except RegistryAmbiguousError:
            await message.answer(
                "В таблице есть несколько разных записей с вашим Telegram-ником. "
                "Автоматически связывать их небезопасно: заполните профиль вручную."
            )
            return False
        except Exception:
            LOGGER.exception("Google Sheets registry lookup failed")
            return False
        if match is None:
            return False
        if not registry_link_is_trusted(match, message.from_user.id):
            await message.answer(
                "Я нашёл старую запись с таким Telegram-ником, но в ней нет "
                "подтверждённого numeric Telegram ID. Персональные данные и "
                "доступы не показаны: безопасная автоматическая привязка невозможна."
            )
            return False
        link_id = secrets.token_urlsafe(8)
        data = registry_match_data(match)
        data["link_id"] = link_id
        await state.set_data(data)
        await state.set_state(Form.registry_confirm)
        await message.answer(
            registry_confirmation_text(match),
            reply_markup=registry_confirmation_keyboard(link_id),
        )
        return True

    @router.message(CommandStart())
    @router.message(Command("menu"))
    async def start(message: Message, state: FSMContext) -> None:
        await state.clear()
        if message.chat.type != ChatType.PRIVATE:
            await message.answer(
                "Для получения доступа напишите боту в личные сообщения."
            )
            return
        await message.answer(
            "Главное меню обновлено. Здесь можно открыть профиль, посмотреть "
            "серверы и получить доступ.",
            reply_markup=MAIN_MENU,
        )
        if message.from_user is not None:
            registry = await asyncio.to_thread(
                service.storage.registry_match_for, message.from_user.id
            )
            if registry is None:
                await offer_registry_link(message, state)

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
                    "Профиль сохранён.",
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

    @router.callback_query(
        Form.registry_confirm, F.data.startswith("registry:confirm:")
    )
    async def confirm_registry_link(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        link_id = (callback.data or "").rsplit(":", 1)[-1]
        if data.get("link_id") != link_id:
            await callback.answer("Эта привязка уже неактуальна.", show_alert=True)
            return
        match = registry_match_from_data(data)
        current_username = normalize_telegram_username(
            callback.from_user.username or ""
        )
        if current_username != match.telegram_username:
            await callback.answer(
                "Telegram-ник изменился. Запустите проверку заново.",
                show_alert=True,
            )
            return
        if not registry_link_is_trusted(match, callback.from_user.id):
            await state.clear()
            await callback.answer(
                "Telegram ID не совпадает с записью.", show_alert=True
            )
            return
        try:
            await asyncio.to_thread(
                service.storage.save_registry_match,
                callback.from_user.id,
                callback.from_user.username,
                match,
            )
        except RegistryAlreadyClaimedError:
            await state.clear()
            await callback.answer(
                "Эта запись уже связана с другим Telegram ID.", show_alert=True
            )
            if callback.message:
                await callback.message.edit_text(
                    "Автоматическая привязка заблокирована. "
                    "Обратитесь к администратору для ручной проверки."
                )
            return
        await state.clear()
        await callback.answer("Данные связаны")
        if callback.message:
            await callback.message.edit_text("Данные из таблицы привязаны к профилю.")
            profile = await asyncio.to_thread(
                service.storage.profile_for, callback.from_user.id
            )
            if profile is not None:
                await answer_profile(callback.message, profile)

    @router.callback_query(Form.registry_confirm, F.data.startswith("registry:reject:"))
    async def reject_registry_link(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        link_id = (callback.data or "").rsplit(":", 1)[-1]
        if data.get("link_id") != link_id:
            await callback.answer("Эта привязка уже неактуальна.", show_alert=True)
            return
        await state.clear()
        await callback.answer()
        if callback.message:
            await callback.message.edit_text(
                "Привязка отменена. Заполните профиль вручную."
            )
            await callback.message.answer(
                "Откройте форму профиля.", reply_markup=profile_form_menu()
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

    @router.callback_query(F.data == "profile:edit:full_name")
    async def edit_full_name(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(Form.edit_full_name)
        await callback.answer()
        if callback.message:
            await callback.message.answer("Пришлите новое имя и фамилию.")

    @router.callback_query(F.data == "profile:edit:recommender")
    async def edit_recommender(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(Form.edit_recommender)
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "Пришлите имя или Telegram-ник нового рекомендателя."
            )

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
            created_accesses = await asyncio.to_thread(service.submit, application)
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
        if created_accesses:
            status = "active: " + ", ".join(access.host for access in accesses)
            try:
                await asyncio.to_thread(sheets.upsert, application, status)
            except Exception:
                LOGGER.exception("Google Sheets synchronization failed")
        registry, registered_services = await asyncio.gather(
            asyncio.to_thread(
                service.storage.registry_match_for,
                application.telegram_user_id,
            ),
            asyncio.to_thread(
                service.storage.registry_services_for,
                application.telegram_user_id,
            ),
        )
        await state.clear()
        secret_message = await callback.message.answer(
            f"{server_dashboard_text(accesses, registry)}\n\n"
            f"Сообщение удалится через {credential_ttl // 60} мин.",
            protect_content=True,
            reply_markup=server_dashboard_keyboard(accesses, registered_services),
        )
        asyncio.create_task(
            delete_later(
                bot, secret_message.chat.id, secret_message.message_id, credential_ttl
            )
        )

    @router.message(Command("servers"))
    @router.message(F.text.in_({"Мои серверы", "🔐 Мои серверы"}))
    async def my_servers(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.from_user is None or message.chat.type != ChatType.PRIVATE:
            return
        await state.clear()
        accesses = await asyncio.to_thread(
            service.credentials_for, message.from_user.id
        )
        registry = await asyncio.to_thread(
            service.storage.registry_match_for, message.from_user.id
        )
        secret_message = await message.answer(
            f"{server_dashboard_text(accesses, registry)}\n\n"
            f"Сообщение удалится через {credential_ttl // 60} мин.",
            protect_content=True,
            reply_markup=server_dashboard_keyboard(
                accesses, registry.servers if registry else ()
            ),
        )
        asyncio.create_task(
            delete_later(
                bot, secret_message.chat.id, secret_message.message_id, credential_ttl
            )
        )

    @router.message(Command("profile"))
    @router.message(Command("myinfo"))
    @router.message(F.text == "👤 Мой профиль")
    @router.message(F.text == "🪪 My Info")
    async def my_profile(message: Message, state: FSMContext) -> None:
        if message.from_user is None or message.chat.type != ChatType.PRIVATE:
            return
        await state.clear()
        profile, registry = await asyncio.gather(
            asyncio.to_thread(service.storage.profile_for, message.from_user.id),
            asyncio.to_thread(service.storage.registry_match_for, message.from_user.id),
        )
        if registry is None and await offer_registry_link(message, state):
            return
        if profile is None:
            await message.answer(
                "Профиль пока не заполнен.", reply_markup=profile_form_menu()
            )
            return
        await answer_profile(message, profile)

    @router.message(F.text == "🖥 Доступные серверы")
    async def available_servers(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(available_servers_text(), reply_markup=MAIN_MENU)

    @router.message(F.text == "📚 Сайт и правила")
    async def site_and_rules(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(
            f'Правила и инструкции: <a href="{RULES_URL}">{RULES_URL}</a>',
            reply_markup=MAIN_MENU,
        )

    @router.callback_query(F.data.startswith("servers:add:"))
    async def add_server(callback: CallbackQuery, state: FSMContext) -> None:
        host = (callback.data or "").rsplit(":", 1)[-1]
        if host not in SERVER_NAMES or callback.message is None:
            await callback.answer("Неизвестный сервер.", show_alert=True)
            return
        await state.clear()
        await callback.answer()
        await callback.message.answer(
            f"Откройте короткую заявку на {SERVER_NAMES[host]}.",
            reply_markup=server_request_menu(host),
        )

    @router.message(F.text.in_({"Главное меню", "Открыть форму доступа"}))
    async def main_menu(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Главное меню обновлено.", reply_markup=MAIN_MENU)

    @router.message(Form.edit_full_name, F.text)
    async def save_full_name(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        profile = await asyncio.to_thread(
            service.storage.profile_for, message.from_user.id
        )
        if profile is None:
            await state.clear()
            await message.answer(
                "Профиль не найден. Заполните его заново.",
                reply_markup=profile_form_menu(),
            )
            return
        updated = Profile(
            profile.telegram_user_id,
            message.from_user.username,
            message.text.strip(),
            profile.recommender,
        )
        try:
            validate_profile(updated)
        except ValidationError as error:
            await message.answer(str(error))
            return
        await asyncio.to_thread(service.storage.save_profile, updated)
        await state.clear()
        await answer_profile(message, updated)

    @router.message(Form.edit_recommender, F.text)
    async def save_recommender(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        profile = await asyncio.to_thread(
            service.storage.profile_for, message.from_user.id
        )
        if profile is None:
            await state.clear()
            await message.answer(
                "Профиль не найден. Заполните его заново.",
                reply_markup=profile_form_menu(),
            )
            return
        updated = Profile(
            profile.telegram_user_id,
            message.from_user.username,
            profile.full_name,
            message.text.strip(),
        )
        try:
            validate_profile(updated)
        except ValidationError as error:
            await message.answer(str(error))
            return
        await asyncio.to_thread(service.storage.save_profile, updated)
        await state.clear()
        await answer_profile(message, updated)

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
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Открыть главное меню"),
            BotCommand(command="menu", description="Обновить главное меню"),
            BotCommand(command="myinfo", description="Показать профиль"),
            BotCommand(command="profile", description="Показать профиль"),
            BotCommand(command="servers", description="Показать мои серверы"),
            BotCommand(command="cancel", description="Отменить текущее действие"),
        ]
    )
    await dispatcher.start_polling(
        bot, allowed_updates=dispatcher.resolve_used_update_types()
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run(Settings.from_env()))
