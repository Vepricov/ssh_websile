import asyncio
import contextlib
import html
import logging
import secrets
import time
from collections.abc import Sequence
from urllib.parse import urlencode, urlparse

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
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
from .disk_monitoring import (
    DiskMonitor,
    DiskScanner,
    format_disk_status,
    run_disk_monitor,
)
from .domain import (
    Application,
    GpuProcessAction,
    Profile,
    ProvisionedAccess,
    RegistryMatch,
    ServerRequest,
)
from .monitoring import (
    PUBLIC_SERVER_NAMES,
    GpuFleetScanner,
    GpuIdleMonitor,
    GpuIdlePolicy,
    GpuStatusScanner,
    format_gpu_status,
    run_gpu_idle_monitor,
)
from .process_control import (
    GpuProcessController,
    ProcessControlError,
    signal_gpu_process_action,
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
    "<b>Как получить VPN для H200</b>\n"
    "Перед подключением получите VPN:\n"
    "1. Пришлите ФИО и физтеховскую почту Диме "
    '<a href="https://t.me/undef1ne5">@undef1ne5</a> с просьбой оформить VPN.\n'
    "2. Дождитесь письма на физтеховскую почту. Отправитель: "
    "<code>helpdesk02@vkusvill.ru</code>.\n"
    "3. После получения VPN вернитесь в бот и отправьте заявку через "
    "«➕ Получить доступ».\n"
    "Без VPN подключиться к H200 по SSH нельзя. Для остальных серверов "
    "можно сразу отправлять заявку."
)
SERVER_NAMES = {
    "shkodnik_opt": "Старый сервер (MIPT)",
    "brain_lab": "Новый сервер (BrainLab)",
    "vv_h200": "H200 (VV)",
}
SERVICE_NAMES = {"mlsub": "mlsub / Cloud.ru"}
ALL_SERVICE_NAMES = {
    **SERVER_NAMES,
    **SERVICE_NAMES,
    "yandex": "Яндекс-сервер",
}
BOT_DEFAULTS = DefaultBotProperties(
    parse_mode=ParseMode.HTML,
    link_preview_is_disabled=True,
)


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
            KeyboardButton(text="🖥 Все серверы лаборатории"),
            KeyboardButton(
                text="➕ Получить доступ", web_app=WebAppInfo(url=SERVER_FORM_URL)
            ),
        ],
        [
            KeyboardButton(text="📚 Сайт и правила"),
            KeyboardButton(text="📡 Статус серверов"),
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


def admin_approval_keyboard(request_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Одобрить", callback_data=f"admin:approve:{request_id}"
                ),
                InlineKeyboardButton(
                    text="Отклонить", callback_data=f"admin:reject:{request_id}"
                ),
            ]
        ]
    )


def gpu_stop_confirmation_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, отправить SIGTERM",
                    callback_data=f"gpu:term:{token}",
                ),
                InlineKeyboardButton(
                    text="Отмена", callback_data=f"gpu:cancel:{token}"
                ),
            ]
        ]
    )


def gpu_force_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Завершить принудительно",
                    callback_data=f"gpu:kill:{token}",
                ),
                InlineKeyboardButton(
                    text="Отмена", callback_data=f"gpu:cancel:{token}"
                ),
            ]
        ]
    )


def gpu_action_text(action: GpuProcessAction) -> str:
    server_name = PUBLIC_SERVER_NAMES.get(action.host, action.host)
    pids = ", ".join(str(item.pid) for item in action.identities)
    return (
        f"<b>{html.escape(server_name)}</b>\n"
        f"Unix username: <code>{html.escape(action.server_username)}</code>\n"
        f"PID: <code>{pids}</code>"
    )


def gpu_sudo_passwords(
    storage: Storage, admin_user_ids: frozenset[int]
) -> dict[str, str]:
    passwords: dict[str, str] = {}
    for telegram_user_id in sorted(admin_user_ids):
        for access in storage.credentials_for(telegram_user_id):
            if access.host not in SERVER_NAMES or not access.password:
                continue
            existing = passwords.get(access.host)
            if existing is not None and existing != access.password:
                raise RuntimeError(
                    f"conflicting admin sudo credentials for {access.host}"
                )
            passwords[access.host] = access.password
    return passwords


def server_dashboard_keyboard(
    accesses: Sequence[ProvisionedAccess],
    registered_services: Sequence[str] = (),
) -> InlineKeyboardMarkup | None:
    existing = {access.host for access in accesses} | set(registered_services)
    rows = [
        [InlineKeyboardButton(text=f"➕ {name}", callback_data=f"servers:add:{host}")]
        for host, name in {**SERVER_NAMES, **SERVICE_NAMES}.items()
        if host not in existing and (host != "mlsub" or "brain_lab" in existing)
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def server_request_menu(host: str) -> ReplyKeyboardMarkup:
    if host not in {**SERVER_NAMES, **SERVICE_NAMES}:
        raise ValueError(f"unknown server: {host}")
    url = f"{SERVER_FORM_URL}?{urlencode({'server': host})}"
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text=f"Открыть заявку: {ALL_SERVICE_NAMES[host]}",
                    web_app=WebAppInfo(url=url),
                )
            ],
            [KeyboardButton(text="Главное меню")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def demo_server_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="Открыть demo-форму серверов",
                    web_app=WebAppInfo(url=SERVER_FORM_URL),
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


def request_requires_admin(
    application: Application,
    *,
    user_is_known: bool,
    mlsub_is_missing: bool = True,
) -> bool:
    return not user_is_known or (mlsub_is_missing and "mlsub" in application.servers)


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
    selected = [ALL_SERVICE_NAMES[str(host)] for host in data.get("servers", [])]
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
        "<b>Все серверы лаборатории</b>\n\n"
        "<b>Старый сервер (MIPT)</b>\n"
        "4 × RTX 2080 11 GB, 2 × A100 40 GB, 2 × A100 80 GB\n\n"
        "<b>Новый сервер (BrainLab)</b>\n"
        "4 × A100 80 GB\n\n"
        "<b>H200 (VV)</b>\n"
        "8 × H200 140 GB. Перед заявкой пришлите ФИО и физтеховскую "
        'почту <a href="https://t.me/undef1ne5">@undef1ne5</a>. '
        "Дождитесь письма от <code>helpdesk02@vkusvill.ru</code>, затем "
        "вернитесь в бот и отправьте заявку через «➕ Получить доступ». "
        "Без VPN подключение по SSH недоступно.\n\n"
        "<b>mlsub / Cloud.ru</b>\n"
        "отдельный вычислительный сервис для запуска задач в очереди Cloud.ru. "
        "Для заявки нужен уже выданный доступ к BrainLab. mlsub включается "
        "только после отдельного одобрения администратора.\n\n"
        "<b>Яндекс-сервер</b>\n"
        'Для получения доступа напишите <a href="https://t.me/aloeme">@aloeme</a>.\n\n'
        "<b>Alibaba Ascend</b>\n"
        "тестируем. Выдача доступа пока не открыта."
    )


def onboarding_text() -> str:
    return (
        "<b>BRAIn Lab · доступ к вычислениям</b>\n\n"
        "Сначала заполните <b>Мой профиль</b>: имя и кто вас пригласил. "
        "Затем откройте <b>Все серверы лаборатории</b>, чтобы сравнить ресурсы и "
        "условия подключения.\n\n"
        "Через <b>Получить доступ</b> можно отправить заявку на сервер или "
        "отдельный сервис. В <b>Мои серверы</b> бот показывает ваши логины, "
        "команды подключения и сохранённые пароли."
    )


def demo_accesses(
    username: str, requested_services: Sequence[str]
) -> list[ProvisionedAccess]:
    services = list(requested_services)
    connections = {
        "shkodnik_opt": f"ssh -p 10096 {username}@proxy2.cod.phystech.edu",
        "brain_lab": f"ssh -p 10210 {username}@proxy2.cod.phystech.edu",
        "vv_h200": f"ssh -p 30018 {username}@10.0.116.11",
        "mlsub": "mlsub run --repo <https git> --entry train.py --gpus 1",
    }
    return [
        ProvisionedAccess(
            host=host,
            username=username,
            password=None if host == "mlsub" else "DEMO-PASSWORD",
            connection=connections[host],
            group_url="",
            created=False,
        )
        for host in services
    ]


def available_servers_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="☁️ mlsub / Cloud.ru", callback_data="catalog:mlsub"
                )
            ],
            [
                InlineKeyboardButton(
                    text="◌ Alibaba Ascend · тестируем",
                    callback_data="catalog:ascend:soon",
                )
            ],
        ]
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


def registry_link_can_be_claimed(
    match: RegistryMatch, telegram_user_id: int
) -> bool:
    return match.telegram_user_id in (None, telegram_user_id)


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
    text = (
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
    if registry is not None:
        text += (
            "\nПароли к старым доступам: ищите в переписке с "
            "@shkodnik1917."
        )
    return text


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
                username_line = (
                    f"Ник: <code>{html.escape(registry.server_username)}</code>"
                    if registry.server_username
                    else "Ник: не указан в таблице"
                )
                sections.append(
                    f"✅ <b>{html.escape(name)}</b>\n"
                    f"{username_line}\n"
                    "Доступ найден в таблице.\n"
                    "Пароль: ищите в переписке с @shkodnik1917."
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
            lines.append(VPN_NOTE)
        sections.append("\n".join(lines))
    mlsub = by_host.get("mlsub")
    if mlsub is not None:
        sections.append(
            "✅ <b>mlsub / Cloud.ru</b>\n"
            f"Ник: <code>{html.escape(mlsub.username)}</code>\n"
            "Отдельный сервис очереди; пароль используется от BrainLab.\n"
            "Команды: <code>mlsub run | list | status | logs | kill</code>"
        )
    elif "mlsub" in registered:
        sections.append(
            "✅ <b>mlsub / Cloud.ru</b>\n"
            "Доступ найден в таблице. Пароль используется от BrainLab."
        )
    else:
        sections.append(
            "○ <b>mlsub / Cloud.ru</b>\n"
            "Сначала получите доступ к BrainLab, затем отправьте отдельную заявку."
        )
    sections.append(
        "☁️ <b>Яндекс-сервер</b>\n"
        'Для получения доступа напишите <a href="https://t.me/aloeme">@aloeme</a>.'
    )
    sections.append(f'Правила: <a href="{RULES_URL}">открыть сайт</a>')
    return "\n\n".join(sections)


def build_router(
    service: AccessService,
    sheets: GoogleSheetsRepository,
    demo_user_ids: frozenset[int] = frozenset(),
    admin_user_ids: frozenset[int] = frozenset(),
    process_controller: GpuProcessController | None = None,
    gpu_status_scanner: GpuStatusScanner | None = None,
    disk_scanner: DiskScanner | None = None,
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
        if not registry_link_can_be_claimed(match, message.from_user.id):
            await message.answer(
                "Запись с вашим Telegram-ником уже связана с другим Telegram ID. "
                "Автоматическая привязка заблокирована: обратитесь к администратору."
            )
            return False
        legacy_claim = match.telegram_user_id is None
        try:
            await asyncio.to_thread(
                service.storage.save_registry_match,
                message.from_user.id,
                message.from_user.username,
                match,
                allow_legacy_claim=legacy_claim,
            )
        except RegistryAlreadyClaimedError:
            await message.answer(
                "Эта запись уже связана с другим Telegram-аккаунтом. "
                "Обратитесь к администратору для ручной проверки."
            )
            return False
        if legacy_claim:
            try:
                await asyncio.to_thread(
                    sheets.claim_registry_user_id,
                    message.from_user.username,
                    message.from_user.id,
                )
            except Exception:
                LOGGER.exception(
                    "Could not persist Telegram ID for registry user %s",
                    message.from_user.id,
                )
        await state.clear()
        profile = await asyncio.to_thread(
            service.storage.profile_for, message.from_user.id
        )
        await message.answer(
            "Нашёл ваши данные в таблице и привязал их к Telegram-профилю."
        )
        if profile is not None:
            await answer_profile(message, profile)
        return True

    async def provision_application(
        chat_id: int,
        user: User,
        application: Application,
        bot: Bot,
    ) -> bool:
        try:
            created_accesses = await asyncio.to_thread(service.submit, application)
            accesses = await asyncio.to_thread(
                service.credentials_for, application.telegram_user_id
            )
        except ValidationError as error:
            await bot.send_message(chat_id, str(error), reply_markup=MAIN_MENU)
            return False
        except ProvisioningError:
            LOGGER.exception("Provisioning failed for Telegram user %s", user.id)
            await bot.send_message(
                chat_id,
                "Не удалось создать доступ. Проверьте «Мои серверы»: часть "
                "ресурсов могла быть выдана до ошибки. Попробуйте ещё раз позже.",
                reply_markup=MAIN_MENU,
            )
            return False
        if created_accesses:
            status = "active: " + ", ".join(access.host for access in accesses)
            try:
                await asyncio.to_thread(sheets.upsert, application, status)
            except Exception:
                LOGGER.exception("Google Sheets synchronization failed")
        registry, registered_services = await asyncio.gather(
            asyncio.to_thread(
                service.storage.registry_match_for, application.telegram_user_id
            ),
            asyncio.to_thread(
                service.storage.registry_services_for, application.telegram_user_id
            ),
        )
        await bot.send_message(
            chat_id,
            server_dashboard_text(accesses, registry),
            protect_content=True,
            reply_markup=server_dashboard_keyboard(accesses, registered_services),
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
        await message.answer(onboarding_text(), reply_markup=MAIN_MENU)
        if message.from_user is not None:
            registry = await asyncio.to_thread(
                service.storage.registry_match_for, message.from_user.id
            )
            if registry is None and await offer_registry_link(message, state):
                return
            profile = await asyncio.to_thread(
                service.storage.profile_for, message.from_user.id
            )
            if profile is not None:
                await answer_profile(message, profile)

    @router.message(Command("demo"))
    async def demo(message: Message, state: FSMContext) -> None:
        if message.from_user is None or message.chat.type != ChatType.PRIVATE:
            return
        await state.clear()
        if message.from_user.id not in demo_user_ids:
            await message.answer("Demo-режим доступен только владельцу бота.")
            return
        await state.set_data({"demo": True})
        await message.answer(
            "<b>Безопасная проверка onboarding</b>\n\n"
            "Вы пройдёте те же формы, что новый пользователь. Бот не изменит "
            "ваш профиль, таблицу или Unix-аккаунты, а пароль будет явно "
            "помечен как демонстрационный.",
            reply_markup=profile_form_menu(),
        )

    @router.message(F.web_app_data)
    async def web_app_form(message: Message, state: FSMContext, bot: Bot) -> None:
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
            state_data = await state.get_data()
            is_demo = (
                state_data.get("demo") is True
                and message.from_user.id in demo_user_ids
            )
            if isinstance(submission, Profile):
                validate_profile(submission)
                if is_demo:
                    await state.set_data(
                        {
                            "demo": True,
                            "demo_full_name": submission.full_name,
                            "demo_recommender": submission.recommender,
                        }
                    )
                    await message.answer(
                        "Demo-профиль принят. Теперь выберите ресурсы.",
                        reply_markup=demo_server_menu(),
                    )
                    return
                await asyncio.to_thread(service.storage.save_profile, submission)
                await state.clear()
                await message.answer(
                    "Профиль сохранён.",
                    reply_markup=MAIN_MENU,
                )
                return
            if is_demo and state_data.get("demo_full_name"):
                profile = Profile(
                    message.from_user.id,
                    message.from_user.username,
                    str(state_data["demo_full_name"]),
                    str(state_data["demo_recommender"]),
                )
            else:
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
        if is_demo:
            accesses = demo_accesses(
                application.server_username, application.servers
            )
            await state.clear()
            await message.answer(
                "<b>DEMO · реальные серверы не изменялись</b>\n\n"
                + server_dashboard_text(accesses)
                + "\n\nПароль <code>DEMO-PASSWORD</code> ненастоящий.",
                protect_content=True,
                reply_markup=MAIN_MENU,
            )
            return
        try:
            await asyncio.to_thread(service.validate_request, application)
        except ValidationError as error:
            await message.answer(str(error), reply_markup=MAIN_MENU)
            return
        known_user = await asyncio.to_thread(
            service.storage.application_for, message.from_user.id
        )
        registry = await asyncio.to_thread(
            service.storage.registry_match_for, message.from_user.id
        )
        mlsub_is_missing = not await asyncio.to_thread(
            service.storage.has_access, message.from_user.id, "mlsub"
        ) and not await asyncio.to_thread(
            service.storage.has_registered_service,
            message.from_user.id,
            "mlsub",
        )
        await state.clear()
        if not request_requires_admin(
            application,
            user_is_known=known_user is not None or registry is not None,
            mlsub_is_missing=mlsub_is_missing,
        ):
            await provision_application(
                message.chat.id, message.from_user, application, bot
            )
            return
        if not admin_user_ids:
            await message.answer(
                "Новые заявки временно не принимаются: администратор не настроен.",
                reply_markup=MAIN_MENU,
            )
            return
        request_id = secrets.token_urlsafe(8)
        await asyncio.to_thread(
            service.storage.save_pending_request, request_id, application
        )
        for admin_id in admin_user_ids:
            try:
                await bot.send_message(
                    admin_id,
                    (
                        "<b>Отдельная заявка на mlsub</b>\n\n"
                        if "mlsub" in application.servers
                        else "<b>Новая заявка на доступ</b>\n\n"
                    )
                    + application_summary(application_data(application)),
                    reply_markup=admin_approval_keyboard(request_id),
                )
            except Exception:
                LOGGER.exception("Could not notify access admin %s", admin_id)
        await message.answer(
            "Заявка отправлена администратору. Больше ничего заполнять не нужно: "
            "после одобрения бот сам пришлёт доступы.",
            reply_markup=MAIN_MENU,
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
        await provision_application(
            callback.from_user.id,
            callback.from_user,
            application_from_data(callback.from_user, data),
            bot,
        )

    @router.callback_query(F.data.startswith("admin:approve:"))
    async def approve_new_user(callback: CallbackQuery, bot: Bot) -> None:
        if callback.from_user.id not in admin_user_ids:
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        request_id = (callback.data or "").rsplit(":", 1)[-1]
        application = await asyncio.to_thread(
            service.storage.claim_pending_request, request_id
        )
        if application is None:
            await callback.answer("Заявка уже обработана.", show_alert=True)
            return
        await callback.answer("Выдаю доступ…")
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
        applicant = User(
            id=application.telegram_user_id,
            is_bot=False,
            first_name=application.full_name,
            username=application.telegram_username,
        )
        success = await provision_application(
            application.telegram_user_id, applicant, application, bot
        )
        await asyncio.to_thread(
            service.storage.resolve_pending_request,
            request_id,
            "approved" if success else "failed",
        )
        if callback.message:
            await callback.message.answer(
                "Заявка одобрена, доступы отправлены пользователю."
                if success
                else "При выдаче произошла ошибка; пользователю отправлено уведомление."
            )

    @router.callback_query(F.data.startswith("admin:reject:"))
    async def reject_new_user(callback: CallbackQuery, bot: Bot) -> None:
        if callback.from_user.id not in admin_user_ids:
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        request_id = (callback.data or "").rsplit(":", 1)[-1]
        application = await asyncio.to_thread(
            service.storage.claim_pending_request, request_id
        )
        if application is None:
            await callback.answer("Заявка уже обработана.", show_alert=True)
            return
        await asyncio.to_thread(
            service.storage.resolve_pending_request, request_id, "rejected"
        )
        await bot.send_message(
            application.telegram_user_id,
            "Заявка на серверный доступ отклонена. Уточните детали у рекомендателя.",
            reply_markup=MAIN_MENU,
        )
        await callback.answer("Заявка отклонена")
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)

    @router.callback_query(F.data.startswith("gpu:stop:"))
    async def confirm_gpu_stop(callback: CallbackQuery) -> None:
        if process_controller is None or callback.message is None:
            await callback.answer("Управление процессами недоступно.", show_alert=True)
            return
        token = (callback.data or "").rsplit(":", 1)[-1]
        now = int(time.time())
        action = await asyncio.to_thread(
            service.storage.gpu_process_action_for,
            token,
            callback.from_user.id,
            now=now,
        )
        if action is None or action.status != "pending":
            await callback.answer("Кнопка устарела или уже использована.", show_alert=True)
            return
        try:
            alive = await asyncio.to_thread(
                process_controller.alive,
                action.host,
                action.server_username,
                action.identities,
            )
        except ProcessControlError:
            LOGGER.exception("Could not verify GPU process action %s", token)
            await callback.answer("Не удалось проверить процесс.", show_alert=True)
            return
        if alive != action.identities:
            await asyncio.to_thread(
                service.storage.transition_gpu_process_action,
                token,
                callback.from_user.id,
                "pending",
                "stale",
                now=now,
            )
            await callback.answer(
                "Процесс уже завершился или PID изменился.", show_alert=True
            )
            return
        await callback.answer()
        await callback.message.answer(
            "<b>Точно остановить эти процессы?</b>\n\n"
            + gpu_action_text(action)
            + "\n\nСначала бот отправит безопасный SIGTERM.",
            reply_markup=gpu_stop_confirmation_keyboard(token),
        )

    @router.callback_query(F.data.startswith("gpu:term:"))
    async def terminate_gpu_process(callback: CallbackQuery) -> None:
        if process_controller is None or callback.message is None:
            await callback.answer("Управление процессами недоступно.", show_alert=True)
            return
        token = (callback.data or "").rsplit(":", 1)[-1]
        now = int(time.time())
        try:
            action = await asyncio.to_thread(
                signal_gpu_process_action,
                service.storage,
                process_controller,
                token,
                callback.from_user.id,
                "TERM",
                now=now,
            )
        except ProcessControlError:
            LOGGER.exception("Could not send SIGTERM for GPU action %s", token)
            await callback.answer(
                "Действие устарело или процесс изменился.", show_alert=True
            )
            return
        await callback.answer("SIGTERM отправлен")
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            "SIGTERM отправлен. Через 10 секунд проверю процесс.\n\n"
            + gpu_action_text(action)
        )
        await asyncio.sleep(10)
        current = await asyncio.to_thread(
            service.storage.gpu_process_action_for,
            token,
            callback.from_user.id,
            now=int(time.time()),
        )
        if current is None or current.status != "term_sent":
            return
        try:
            alive = await asyncio.to_thread(
                process_controller.alive,
                current.host,
                current.server_username,
                current.identities,
            )
        except ProcessControlError:
            LOGGER.exception("Could not verify SIGTERM result for %s", token)
            await callback.message.answer(
                "Не удалось проверить результат SIGTERM. Попробуйте позже."
            )
            return
        if not alive:
            await asyncio.to_thread(
                service.storage.transition_gpu_process_action,
                token,
                callback.from_user.id,
                "term_sent",
                "completed",
                now=int(time.time()),
            )
            await callback.message.answer("✅ Процесс остановлен, SIGKILL не нужен.")
            return
        await callback.message.answer(
            "Процесс всё ещё работает. Принудительное завершение необратимо.",
            reply_markup=gpu_force_keyboard(token),
        )

    @router.callback_query(F.data.startswith("gpu:kill:"))
    async def kill_gpu_process(callback: CallbackQuery) -> None:
        if process_controller is None or callback.message is None:
            await callback.answer("Управление процессами недоступно.", show_alert=True)
            return
        token = (callback.data or "").rsplit(":", 1)[-1]
        try:
            action = await asyncio.to_thread(
                signal_gpu_process_action,
                service.storage,
                process_controller,
                token,
                callback.from_user.id,
                "KILL",
                now=int(time.time()),
            )
        except ProcessControlError:
            LOGGER.exception("Could not send SIGKILL for GPU action %s", token)
            await callback.answer(
                "Действие недоступно или процесс изменился.", show_alert=True
            )
            return
        await callback.answer("SIGKILL отправлен")
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            "Процессу отправлен SIGKILL.\n\n" + gpu_action_text(action)
        )

    @router.callback_query(F.data.startswith("gpu:cancel:"))
    async def cancel_gpu_process_action(callback: CallbackQuery) -> None:
        token = (callback.data or "").rsplit(":", 1)[-1]
        now = int(time.time())
        action = await asyncio.to_thread(
            service.storage.gpu_process_action_for,
            token,
            callback.from_user.id,
            now=now,
        )
        if action is None or action.status not in {"pending", "term_sent"}:
            await callback.answer("Кнопка уже неактуальна.", show_alert=True)
            return
        await asyncio.to_thread(
            service.storage.transition_gpu_process_action,
            token,
            callback.from_user.id,
            action.status,
            "canceled",
            now=now,
        )
        await callback.answer("Отменено")
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)

    @router.message(Command("servers"))
    @router.message(F.text.in_({"Мои серверы", "🔐 Мои серверы"}))
    async def my_servers(message: Message, state: FSMContext) -> None:
        if message.from_user is None or message.chat.type != ChatType.PRIVATE:
            return
        await state.clear()
        accesses = await asyncio.to_thread(
            service.credentials_for, message.from_user.id
        )
        registry = await asyncio.to_thread(
            service.storage.registry_match_for, message.from_user.id
        )
        await message.answer(
            server_dashboard_text(accesses, registry),
            protect_content=True,
            reply_markup=server_dashboard_keyboard(
                accesses, registry.servers if registry else ()
            ),
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

    @router.message(
        F.text.in_({"🖥 Все серверы лаборатории", "🖥 Доступные серверы"})
    )
    async def available_servers(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(
            available_servers_text(), reply_markup=available_servers_keyboard()
        )

    @router.message(Command("status"))
    @router.message(F.text == "📡 Статус серверов")
    async def gpu_status(message: Message, state: FSMContext) -> None:
        await state.clear()
        if gpu_status_scanner is None:
            await message.answer("Мониторинг серверов временно недоступен.")
            return
        progress = await message.answer("Проверяю серверы и GPU…")
        statuses = await asyncio.to_thread(gpu_status_scanner.scan)
        await progress.edit_text(format_gpu_status(statuses))

    @router.message(Command("disk_status"))
    async def disk_status(message: Message, state: FSMContext) -> None:
        await state.clear()
        if disk_scanner is None:
            await message.answer("Мониторинг дисков временно недоступен.")
            return
        progress = await message.answer("Проверяю свободное место…")
        statuses = await asyncio.to_thread(disk_scanner.scan)
        await progress.edit_text(format_disk_status(statuses))

    @router.message(Command("bind_server"))
    async def bind_server_chat(message: Message, state: FSMContext) -> None:
        await state.clear()
        if message.from_user is None or message.from_user.id not in admin_user_ids:
            await message.answer("Привязывать серверные чаты может только владелец.")
            return
        if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
            await message.answer(
                "Запустите команду в нужном серверном чате или его теме."
            )
            return
        parts = (message.text or "").split(maxsplit=1)
        host = parts[1].strip() if len(parts) == 2 else ""
        allowed_hosts = set(disk_scanner.hosts) if disk_scanner is not None else set()
        if host not in allowed_hosts:
            await message.answer(
                "Укажите сервер: <code>/bind_server brain_lab</code>, "
                "<code>/bind_server shkodnik_opt</code>, "
                "<code>/bind_server vv_h200</code> или "
                "<code>/bind_server yandex</code>."
            )
            return
        await asyncio.to_thread(
            service.storage.bind_server_chat,
            host,
            message.chat.id,
            message.message_thread_id,
            message.from_user.id,
        )
        await message.answer(
            f"Этот чат привязан к "
            f"<b>{html.escape(PUBLIC_SERVER_NAMES[host])}</b>. "
            "Сюда будут приходить предупреждения о диске."
        )

    @router.message(Command("unbind_server"))
    async def unbind_server_chat(message: Message, state: FSMContext) -> None:
        await state.clear()
        if message.from_user is None or message.from_user.id not in admin_user_ids:
            await message.answer("Отвязывать серверные чаты может только владелец.")
            return
        parts = (message.text or "").split(maxsplit=1)
        host = parts[1].strip() if len(parts) == 2 else ""
        if host not in PUBLIC_SERVER_NAMES:
            await message.answer("Укажите сервер после команды /unbind_server.")
            return
        removed = await asyncio.to_thread(service.storage.unbind_server_chat, host)
        await message.answer("Привязка удалена." if removed else "Привязки не было.")

    @router.callback_query(F.data == "catalog:mlsub")
    async def mlsub_catalog(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "<b>mlsub / Cloud.ru</b>\n\n"
                "Это отдельный сервис очереди задач. Для заявки нужен уже "
                "выданный доступ к BrainLab. Доступ к mlsub включается только "
                "после отдельного одобрения администратора.\n\n"
                "После выдачи доступны команды: <code>mlsub run</code>, "
                "<code>mlsub list</code>, <code>mlsub status</code>, "
                "<code>mlsub logs</code> и <code>mlsub kill</code>.",
                reply_markup=server_request_menu("mlsub"),
            )

    @router.callback_query(F.data == "catalog:ascend:soon")
    async def ascend_soon(callback: CallbackQuery) -> None:
        await callback.answer(
            "Alibaba Ascend сейчас тестируется. Доступ появится позже.",
            show_alert=True,
        )

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
        if host not in {**SERVER_NAMES, **SERVICE_NAMES} or callback.message is None:
            await callback.answer("Неизвестный сервер.", show_alert=True)
            return
        await state.clear()
        await callback.answer()
        await callback.message.answer(
            f"Откройте короткую заявку на {ALL_SERVICE_NAMES[host]}.",
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
    process_controller = GpuProcessController(
        sudo_passwords=gpu_sudo_passwords(storage, settings.admin_user_ids),
        nopasswd_hosts={"yandex"},
    )
    monitored_hosts = tuple(PUBLIC_SERVER_NAMES)
    gpu_status_scanner = GpuStatusScanner(monitored_hosts)
    disk_scanner = DiskScanner(monitored_hosts)
    sheets = GoogleSheetsRepository(
        settings.google_service_account_file,
        settings.spreadsheet_id,
        settings.sheet_id,
    )
    bot = Bot(
        token=settings.bot_token,
        default=BOT_DEFAULTS,
    )
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(
        build_router(
            service,
            sheets,
            settings.demo_user_ids,
            settings.admin_user_ids,
            process_controller,
            gpu_status_scanner,
            disk_scanner,
        )
    )
    monitor_task = None
    disk_monitor_task = None
    if settings.gpu_monitor_enabled:
        policy = GpuIdlePolicy(
            interval_seconds=settings.gpu_monitor_interval_seconds,
            memory_threshold_mb=settings.gpu_idle_memory_threshold_mb,
            utilization_threshold_percent=(
                settings.gpu_idle_utilization_threshold_percent
            ),
            minimum_duration_seconds=settings.gpu_idle_minimum_duration_seconds,
            minimum_samples=settings.gpu_idle_minimum_samples,
            notification_cooldown_seconds=(
                settings.gpu_idle_notification_cooldown_seconds
            ),
        )
        monitor = GpuIdleMonitor(
            storage,
            GpuFleetScanner(
                hosts=monitored_hosts,
                memory_threshold_mb=policy.memory_threshold_mb,
                utilization_threshold_percent=(
                    policy.utilization_threshold_percent
                ),
            ),
            policy,
        )
        monitor_task = asyncio.create_task(
            run_gpu_idle_monitor(
                bot,
                monitor,
                admin_user_ids=settings.admin_user_ids,
                process_controller=process_controller,
            )
        )
    if settings.disk_monitor_enabled:
        disk_monitor = DiskMonitor(
            storage,
            disk_scanner,
            warning_free_percent=settings.disk_warning_free_percent,
            critical_free_percent=settings.disk_critical_free_percent,
            notification_cooldown_seconds=(
                settings.disk_notification_cooldown_seconds
            ),
        )
        disk_monitor_task = asyncio.create_task(
            run_disk_monitor(
                bot,
                disk_monitor,
                disk_scanner,
                interval_seconds=settings.disk_monitor_interval_seconds,
            )
        )
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Открыть главное меню"),
            BotCommand(command="menu", description="Обновить главное меню"),
            BotCommand(command="myinfo", description="Показать профиль"),
            BotCommand(command="profile", description="Показать профиль"),
            BotCommand(command="servers", description="Показать мои серверы"),
            BotCommand(command="status", description="Показать загрузку GPU"),
            BotCommand(command="disk_status", description="Показать свободное место"),
            BotCommand(command="demo", description="Безопасно проверить onboarding"),
            BotCommand(command="cancel", description="Отменить текущее действие"),
        ]
    )
    try:
        await dispatcher.start_polling(
            bot, allowed_updates=dispatcher.resolve_used_update_types()
        )
    finally:
        if monitor_task is not None:
            monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor_task
        if disk_monitor_task is not None:
            disk_monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await disk_monitor_task


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run(Settings.from_env()))
