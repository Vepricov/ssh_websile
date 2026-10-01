import re
import threading
from datetime import date, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from .domain import Application, Profile, ProvisionedAccess
from .storage import Storage


class Provisioner(Protocol):
    def username_available(self, host: str, username: str) -> bool: ...

    def provision(
        self,
        host: str,
        username: str,
        *,
        full_name: str | None = None,
        owner_id: int | None = None,
    ) -> ProvisionedAccess: ...

    def acknowledge(self, host: str, username: str, *, owner_id: int) -> None: ...


class ValidationError(ValueError):
    pass


USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
ALIBABA_USERNAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,30}$")
ALLOWED_HOSTS = frozenset(
    {
        "shkodnik_opt",
        "brain_lab",
        "vv_h200",
        "2_a100_brain",
        "mlsub",
        "alibaba_ascend",
    }
)
MOSCOW_TIME = ZoneInfo("Europe/Moscow")


def _human_name(value: str) -> bool:
    words = value.split()
    return len(words) >= 2 and all(word.replace("-", "").isalpha() for word in words)


def _access_term_ok(value: str) -> bool:
    if value == "бессрочно":
        return True
    try:
        return date.fromisoformat(value) >= datetime.now(MOSCOW_TIME).date()
    except ValueError:
        return False


def validate_profile(profile: Profile) -> None:
    if not _human_name(profile.full_name):
        raise ValidationError("Укажите имя и фамилию.")


def validate(application: Application) -> None:
    validate_profile(
        Profile(
            telegram_user_id=application.telegram_user_id,
            telegram_username=application.telegram_username,
            full_name=application.full_name,
            recommender=application.recommender,
        )
    )
    if not USERNAME_RE.fullmatch(application.server_username):
        raise ValidationError(
            "Некорректный серверный ник: нужны строчные латинские буквы, "
            "цифры, _ или -, максимум 32 символа."
        )
    if not _access_term_ok(application.access_term.strip()):
        raise ValidationError(
            "Укажите корректный срок доступа: сегодняшнюю или будущую дату либо «бессрочно»."
        )
    if not application.servers:
        raise ValidationError("Выберите хотя бы один сервер.")
    unknown = set(application.servers) - ALLOWED_HOSTS
    if unknown:
        raise ValidationError(f"Неизвестный сервер: {', '.join(sorted(unknown))}.")


class AccessService:
    def __init__(
        self,
        storage: Storage,
        provisioner: Provisioner,
        *,
        ascend_group_url: str = "",
    ) -> None:
        self.storage = storage
        self.provisioner = provisioner
        self.ascend_group_url = ascend_group_url
        self._submit_lock = threading.Lock()

    def submit(self, application: Application) -> list[ProvisionedAccess]:
        with self._submit_lock:
            return self._submit_locked(application)

    def grant_external_access(self, application: Application) -> ProvisionedAccess:
        with self._submit_lock:
            if application.servers != ("alibaba_ascend",):
                raise ValidationError("Ручная выдача доступна только для Alibaba.")
            if self.storage.has_access(
                application.telegram_user_id, "alibaba_ascend"
            ):
                existing = next(
                    access
                    for access in self.storage.credentials_for(
                        application.telegram_user_id
                    )
                    if access.host == "alibaba_ascend"
                )
                if existing.username != application.server_username:
                    raise ValidationError(
                        "Alibaba уже связана с другим серверным ником."
                    )
                self._acknowledge_provisioning(
                    application.telegram_user_id, existing
                )
                return existing
            self.validate_request(application)
            if not self.ascend_group_url:
                raise RuntimeError("ASCEND_GROUP_URL is not configured")
            if not self.provisioner.username_available(
                "alibaba_ascend", application.server_username
            ):
                raise ValidationError(
                    "Этот серверный ник уже занят на Alibaba. Выберите другой ник."
                )
            provisioned = self.provisioner.provision(
                "alibaba_ascend",
                application.server_username,
                full_name=application.full_name,
                owner_id=application.telegram_user_id,
            )
            access = ProvisionedAccess(
                host=provisioned.host,
                username=provisioned.username,
                password=provisioned.password,
                connection=provisioned.connection,
                group_url=self.ascend_group_url,
                created=provisioned.created,
            )
            self.storage.save_application(application)
            self.storage.save_access(application.telegram_user_id, access)
            self._acknowledge_provisioning(application.telegram_user_id, access)
            self.storage.record_event(
                application.telegram_user_id,
                "account_created" if access.created else "account_exists",
                "alibaba_ascend",
            )
            return access

    def _acknowledge_provisioning(
        self, telegram_user_id: int, access: ProvisionedAccess
    ) -> None:
        try:
            self.provisioner.acknowledge(
                access.host, access.username, owner_id=telegram_user_id
            )
        except Exception as error:
            self.storage.record_event(
                telegram_user_id,
                "credential_recovery_cleanup_failed",
                access.host,
                type(error).__name__,
            )

    def validate_request(self, application: Application) -> None:
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
        if "mlsub" in application.servers and len(application.servers) != 1:
            raise ValidationError(
                "mlsub оформляется отдельной заявкой. Уберите остальные "
                "серверы и отправьте заявку только на mlsub."
            )
        if (
            "alibaba_ascend" in application.servers
            and not ALIBABA_USERNAME_RE.fullmatch(application.server_username)
        ):
            raise ValidationError(
                "Для Alibaba серверный ник должен содержать от 2 до 31 символа "
                "и начинаться со строчной латинской буквы."
            )
        if (
            "alibaba_ascend" in application.servers
            and len(application.full_name) > 64
        ):
            raise ValidationError(
                "Для Alibaba имя и фамилия должны занимать не больше 64 символов."
            )
        if "alibaba_ascend" in application.servers and (
            self.storage.has_access(application.telegram_user_id, "alibaba_ascend")
            or self.storage.has_registered_service(
                application.telegram_user_id, "alibaba_ascend"
            )
        ):
            raise ValidationError("Доступ к Alibaba уже отмечен как выданный.")
        if (
            "mlsub" in application.servers
            and not self.storage.has_access(application.telegram_user_id, "brain_lab")
            and not self.storage.has_registered_service(
                application.telegram_user_id, "brain_lab"
            )
        ):
            raise ValidationError(
                "Сначала подайте заявку на Новый сервер (BrainLab) и дождитесь "
                "её одобрения. После выдачи доступа вернитесь к заявке на mlsub."
            )

    def _submit_locked(self, application: Application) -> list[ProvisionedAccess]:
        self.validate_request(application)
        if "alibaba_ascend" in application.servers:
            raise ValidationError(
                "Доступ к Alibaba подтверждается администратором вручную."
            )
        requested_hosts = list(application.servers)
        missing_hosts = [
            host
            for host in requested_hosts
            if not self.storage.has_access(application.telegram_user_id, host)
            and not self.storage.has_registered_service(
                application.telegram_user_id, host
            )
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
