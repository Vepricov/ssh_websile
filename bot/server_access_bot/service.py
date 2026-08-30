import re
import threading
from datetime import date, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from .domain import Application, ProvisionedAccess
from .storage import Storage


class Provisioner(Protocol):
    def username_available(self, host: str, username: str) -> bool: ...

    def provision(self, host: str, username: str) -> ProvisionedAccess: ...


class ValidationError(ValueError):
    pass


USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
TELEGRAM_RE = re.compile(r"^@[A-Za-z0-9_]{3,}$")
ALLOWED_HOSTS = frozenset({"shkodnik_opt", "brain_lab", "vv_h200"})
MOSCOW_TIME = ZoneInfo("Europe/Moscow")


def _human_name(value: str) -> bool:
    words = value.split()
    return len(words) >= 2 and all(word.replace("-", "").isalpha() for word in words)


def _recommender_ok(value: str) -> bool:
    if TELEGRAM_RE.fullmatch(value) or _human_name(value):
        return True
    name, separator, handle = value.rpartition(" ")
    return bool(separator and _human_name(name) and TELEGRAM_RE.fullmatch(handle))


def _access_term_ok(value: str) -> bool:
    if value == "бессрочно":
        return True
    try:
        return date.fromisoformat(value) >= datetime.now(MOSCOW_TIME).date()
    except ValueError:
        return False


def validate(application: Application) -> None:
    if not _human_name(application.full_name):
        raise ValidationError("Укажите имя и фамилию.")
    if not USERNAME_RE.fullmatch(application.server_username):
        raise ValidationError(
            "Некорректный серверный ник: нужны строчные латинские буквы, "
            "цифры, _ или -, максимум 32 символа."
        )
    if not _access_term_ok(application.access_term.strip()):
        raise ValidationError(
            "Укажите корректный срок доступа: сегодняшнюю или будущую дату либо «бессрочно»."
        )
    if not _recommender_ok(application.recommender):
        raise ValidationError("Укажите имя или Telegram-ник рекомендателя.")
    if not application.servers:
        raise ValidationError("Выберите хотя бы один сервер.")
    unknown = set(application.servers) - ALLOWED_HOSTS
    if unknown:
        raise ValidationError(f"Неизвестный сервер: {', '.join(sorted(unknown))}.")


class AccessService:
    def __init__(self, storage: Storage, provisioner: Provisioner) -> None:
        self.storage = storage
        self.provisioner = provisioner
        self._submit_lock = threading.Lock()

    def submit(self, application: Application) -> list[ProvisionedAccess]:
        with self._submit_lock:
            return self._submit_locked(application)

    def _submit_locked(self, application: Application) -> list[ProvisionedAccess]:
        validate(application)
        current_username = self.storage.server_username_for(
            application.telegram_user_id
        )
        if (
            current_username is not None
            and current_username != application.server_username
        ):
            raise ValidationError(
                f"Ваш серверный ник уже зафиксирован: {current_username}. "
                "Для его смены обратитесь к администратору."
            )
        missing_hosts = [
            host
            for host in application.servers
            if not self.storage.has_access(application.telegram_user_id, host)
        ]
        unavailable = [
            host
            for host in missing_hosts
            if not self.provisioner.username_available(
                host, application.server_username
            )
        ]
        if unavailable:
            raise ValidationError(
                "Этот серверный ник уже занят на: "
                + ", ".join(unavailable)
                + ". Выберите другой ник."
            )
        self.storage.save_application(application)
        result = []
        for host in missing_hosts:
            access = self.provisioner.provision(host, application.server_username)
            self.storage.save_access(application.telegram_user_id, access)
            self.storage.record_event(
                application.telegram_user_id,
                "account_created" if access.created else "account_exists",
                host,
            )
            result.append(access)
        return result

    def credentials_for(self, telegram_user_id: int) -> list[ProvisionedAccess]:
        return self.storage.credentials_for(telegram_user_id)
