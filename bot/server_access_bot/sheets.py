import re
import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from .domain import Application, Profile, RegistryMatch

FORM_HEADERS = (
    "Отметка времени",
    "Имя и Фамилия",
    "Никнейм на сервере (допустимы только латинские буквы, цифры, подчёркивание и дефис)",
    'Срок доступа (укажите дату окончания доступа, если есть. Если нет — напишите "бессрочно" для бессрочного доступа)',
    "Рекомендатель (кто пригласил вас на сервер?)",
    "Никнейм в Telegram (потребуется для добавления в общую группу)",
    "Вариант Сервера (если вы не знаете на какой сервер вас добавлять, то скорее всего на старый, но лучше уточнить у рекомендателя)",
    "Планируемая деятельность на сервере (например: fine-tuning, CV, майнинг и др.; опционально)",
)
BOT_HEADERS = (
    "Telegram user ID",
    "Статус выдачи",
    "Обновлено ботом",
)
BOT_USERS_SHEET = "Пользователи бота"
BOT_USER_HEADERS = (
    "Telegram user ID",
    "Telegram username",
    "ФИО",
    "Unix username",
    "Срок доступа",
    "Рекомендатель",
    "Деятельность",
    "Серверы в реестре",
    "Статус профиля",
    "Обновлено ботом",
)
BOT_REQUESTS_SHEET = "Заявки бота"
BOT_REQUEST_HEADERS = (
    "Request ID",
    "Telegram user ID",
    "Telegram username",
    "ФИО",
    "Unix username",
    "Запрошенные серверы",
    "Статус заявки",
    "Рекомендатель",
    "Деятельность",
    "Обновлено ботом",
)
SERVER_LABELS = {
    "shkodnik_opt": "Старый сервер (MIPT) -- x4 RTX 2080 11гб, x2 A100 40гб, x2 A100 80гб",
    "brain_lab": "Новый сервер (BrainLab) -- x4 A100 80гб",
    "vv_h200": "H200 (VB) - x8 H200 140гб",
    "2_a100_brain": "2 × A100 (BrainLab) -- x2 A100 80гб, только сеть МФТИ",
    "mlsub": "mlsub / Cloud.ru — отдельный сервис через BrainLab",
    "alibaba_ascend": "Alibaba — внешняя ручная выдача",
}
REGISTRY_SERVER_LABELS = {
    "shkodnik_opt": "Старый сервер (MIPT)",
    "brain_lab": "Новый сервер (BrainLab)",
    "vv_h200": "H200",
    "2_a100_brain": "2 × A100 (BrainLab)",
    "yandex": "Яндекс-сервер",
    "mlsub": "mlsub / Cloud.ru",
    "alibaba_ascend": "Alibaba",
}
REGISTRY_SERVERS = (
    ("shkodnik_opt", ("Старый сервер (MIPT)", "Старый (mipt)")),
    ("brain_lab", ("Новый сервер (BrainLab)", "Новый (BrainLab)")),
    ("vv_h200", ("H200",)),
    ("2_a100_brain", ("2 × A100 (BrainLab)",)),
    ("yandex", ("Яндекс",)),
    ("mlsub", ("mlsub / Cloud.ru",)),
    ("alibaba_ascend", ("Alibaba Ascend", "Alibaba")),
)
SERVER_USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


class RegistryAmbiguousError(ValueError):
    pass


def _cell(row: list[str], index: int) -> str:
    return row[index].strip() if len(row) > index else ""


def normalize_telegram_username(value: str) -> str:
    normalized = value.strip().lower()
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if normalized.startswith(prefix):
            normalized = normalized.removeprefix(prefix).strip("/")
            break
    return normalized.removeprefix("@").strip()


def registry_match_from_rows(
    rows: list[list[str]],
    telegram_username: str,
    trust_telegram_user_id: bool = False,
) -> RegistryMatch | None:
    normalized_username = normalize_telegram_username(telegram_username)
    if not normalized_username:
        return None
    matched = [
        row
        for row in rows
        if normalize_telegram_username(_cell(row, 5)) == normalized_username
    ]
    if not matched:
        return None
    server_usernames = {
        candidate
        for row in matched
        if (candidate := _cell(row, 2).lstrip("@").lower())
        and SERVER_USERNAME_RE.fullmatch(candidate)
    }
    if len(server_usernames) > 1:
        raise RegistryAmbiguousError(
            "В таблице найдено несколько разных профилей с этим Telegram-ником."
        )
    raw_telegram_user_ids = (
        {_cell(row, 8) for row in matched if _cell(row, 8)}
        if trust_telegram_user_id
        else set()
    )
    if len(raw_telegram_user_ids) > 1:
        raise RegistryAmbiguousError(
            "В таблице найдено несколько Telegram ID для одного профиля."
        )
    telegram_user_id = None
    if raw_telegram_user_ids:
        try:
            telegram_user_id = int(next(iter(raw_telegram_user_ids)))
            if telegram_user_id <= 0:
                raise ValueError
        except ValueError as error:
            raise RegistryAmbiguousError(
                "В таблице найден некорректный Telegram ID."
            ) from error
    latest = matched[-1]
    servers = tuple(
        host
        for host, markers in REGISTRY_SERVERS
        if any(
            any(marker.casefold() in _cell(row, 6).casefold() for marker in markers)
            for row in matched
        )
    )
    return RegistryMatch(
        telegram_username=normalized_username,
        full_name=" ".join(_cell(latest, 1).split()),
        server_username=next(iter(server_usernames), ""),
        access_term=_cell(latest, 3),
        recommender=_cell(latest, 4),
        activity=_cell(latest, 7),
        servers=servers,
        source_row_count=len(matched),
        telegram_user_id=telegram_user_id,
    )


def registry_match_from_sheet_values(
    rows: list[list[str]], telegram_username: str
) -> RegistryMatch | None:
    if not rows or tuple(rows[0][:8]) != FORM_HEADERS:
        raise RuntimeError("Google Sheet form headers changed")
    trusted_id_column = len(rows[0]) > 8 and rows[0][8] == BOT_HEADERS[0]
    return registry_match_from_rows(
        rows[1:],
        telegram_username,
        trust_telegram_user_id=trusted_id_column,
    )


def registry_match_by_server_username_from_sheet_values(
    rows: list[list[str]], server_username: str
) -> RegistryMatch | None:
    if not rows or tuple(rows[0][:8]) != FORM_HEADERS:
        raise RuntimeError("Google Sheet form headers changed")
    normalized_server_username = server_username.strip().lstrip("@").casefold()
    if not SERVER_USERNAME_RE.fullmatch(normalized_server_username):
        return None
    telegram_usernames = {
        normalized
        for row in rows[1:]
        if _cell(row, 2).lstrip("@").casefold() == normalized_server_username
        and (normalized := normalize_telegram_username(_cell(row, 5)))
    }
    if not telegram_usernames:
        return None
    if len(telegram_usernames) != 1:
        raise RegistryAmbiguousError(
            "В таблице найдено несколько Telegram-ников для Unix username."
        )
    trusted_id_column = len(rows[0]) > 8 and rows[0][8] == BOT_HEADERS[0]
    return registry_match_from_rows(
        rows[1:],
        next(iter(telegram_usernames)),
        trust_telegram_user_id=trusted_id_column,
    )


def registry_claim_plan_from_sheet_values(
    rows: list[list[str]],
    telegram_username: str,
    telegram_user_id: int,
) -> tuple[RegistryMatch | None, tuple[int, ...]]:
    if not rows or tuple(rows[0][:8]) != FORM_HEADERS:
        raise RuntimeError("Google Sheet registry headers changed")
    id_header = _cell(rows[0], 8)
    if id_header not in ("", BOT_HEADERS[0]):
        raise RuntimeError("Google Sheet Telegram ID column is already in use")
    if telegram_user_id <= 0:
        raise ValueError("Telegram user ID must be positive")
    match = registry_match_from_sheet_values(rows, telegram_username)
    if match is None:
        return None, ()
    if match.telegram_user_id not in (None, telegram_user_id):
        raise RegistryAmbiguousError(
            "Эта запись уже связана с другим Telegram ID."
        )
    normalized_username = normalize_telegram_username(telegram_username)
    row_numbers = tuple(
        row_number
        for row_number, row in enumerate(rows[1:], start=2)
        if normalize_telegram_username(_cell(row, 5)) == normalized_username
        and not _cell(row, 8)
    )
    return replace(match, telegram_user_id=telegram_user_id), row_numbers


def bot_user_values(
    subject: Application | Profile,
    status: str,
    registry_servers: tuple[str, ...] = (),
    now: datetime | None = None,
) -> list[str]:
    timestamp = (now or datetime.now(UTC)).strftime("%d-%m-%Y %H:%M:%S UTC")
    telegram_username = subject.telegram_username or ""
    if telegram_username and not telegram_username.startswith("@"):
        telegram_username = "@" + telegram_username
    server_username = (
        subject.server_username if isinstance(subject, Application) else ""
    )
    access_term = (
        subject.access_term if isinstance(subject, Application) else ""
    )
    activity = subject.activity if isinstance(subject, Application) else ""
    return [
        str(subject.telegram_user_id),
        telegram_username,
        subject.full_name,
        server_username,
        access_term,
        subject.recommender,
        activity,
        "; ".join(REGISTRY_SERVER_LABELS[host] for host in registry_servers),
        status,
        timestamp,
    ]


def bot_request_values(
    request_id: str,
    application: Application,
    status: str,
    now: datetime | None = None,
) -> list[str]:
    timestamp = (now or datetime.now(UTC)).strftime("%d-%m-%Y %H:%M:%S UTC")
    telegram_username = application.telegram_username or ""
    if telegram_username and not telegram_username.startswith("@"):
        telegram_username = "@" + telegram_username
    return [
        request_id,
        str(application.telegram_user_id),
        telegram_username,
        application.full_name,
        application.server_username,
        "; ".join(REGISTRY_SERVER_LABELS[host] for host in application.servers),
        status,
        application.recommender,
        application.activity,
        timestamp,
    ]


def pad_snapshot_values(
    values: list[list[str]],
    previous_row_count: int,
    column_count: int,
) -> list[list[str]]:
    missing_rows = max(0, previous_row_count - len(values))
    return values + [[""] * column_count for _ in range(missing_rows)]


def bot_registry_match_from_rows(
    rows: list[list[str]],
    *,
    telegram_username: str | None = None,
    server_username: str | None = None,
) -> RegistryMatch | None:
    if not rows or tuple(rows[0][: len(BOT_USER_HEADERS)]) != BOT_USER_HEADERS:
        raise RuntimeError("Google Sheet bot registry headers changed")
    normalized_telegram = normalize_telegram_username(telegram_username or "")
    normalized_server = (server_username or "").strip().lstrip("@").casefold()
    matched = [
        row
        for row in rows[1:]
        if (
            normalized_telegram
            and normalize_telegram_username(_cell(row, 1)) == normalized_telegram
        )
        or (
            normalized_server
            and _cell(row, 3).lstrip("@").casefold() == normalized_server
        )
    ]
    if not matched:
        return None
    ids = {_cell(row, 0) for row in matched if _cell(row, 0)}
    if len(ids) != 1:
        raise RegistryAmbiguousError(
            "В реестре бота найдены разные Telegram ID."
        )
    try:
        telegram_user_id = int(next(iter(ids)))
        if telegram_user_id <= 0:
            raise ValueError
    except (StopIteration, ValueError) as error:
        raise RegistryAmbiguousError(
            "В реестре бота найден некорректный Telegram ID."
        ) from error
    row = matched[-1]
    services_cell = _cell(row, 7).casefold()
    servers = tuple(
        host
        for host, markers in REGISTRY_SERVERS
        if any(marker.casefold() in services_cell for marker in markers)
    )
    if not servers:
        return None
    return RegistryMatch(
        telegram_username=normalize_telegram_username(_cell(row, 1)),
        full_name=_cell(row, 2),
        server_username=_cell(row, 3),
        access_term=_cell(row, 4),
        recommender=_cell(row, 5),
        activity=_cell(row, 6),
        servers=servers,
        source_row_count=1,
        telegram_user_id=telegram_user_id,
    )


class GoogleSheetsRepository:
    def __init__(
        self,
        service_account_file: Path,
        spreadsheet_id: str,
        sheet_id: int,
    ) -> None:
        credentials = Credentials.from_service_account_file(
            service_account_file,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        self.service: Any = build(
            "sheets", "v4", credentials=credentials, cache_discovery=False
        )
        self.spreadsheet_id = spreadsheet_id
        self.sheet_id = sheet_id
        self._title: str | None = None
        self._write_lock = threading.RLock()

    def _sheet_title(self) -> str:
        if self._title is not None:
            return self._title
        metadata = (
            self.service.spreadsheets()
            .get(spreadsheetId=self.spreadsheet_id, fields="sheets.properties")
            .execute()
        )
        for sheet in metadata.get("sheets", []):
            properties = sheet.get("properties", {})
            if int(properties.get("sheetId", -1)) == self.sheet_id:
                self._title = str(properties["title"])
                return self._title
        raise RuntimeError(f"sheet gid {self.sheet_id} was not found")

    def _ensure_sheet(self, title: str, headers: tuple[str, ...]) -> str:
        metadata = (
            self.service.spreadsheets()
            .get(spreadsheetId=self.spreadsheet_id, fields="sheets.properties")
            .execute()
        )
        titles = {
            str(sheet.get("properties", {}).get("title", ""))
            for sheet in metadata.get("sheets", [])
        }
        if title not in titles:
            self.service.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={
                    "requests": [
                        {
                            "addSheet": {
                                "properties": {
                                    "title": title,
                                    "gridProperties": {"frozenRowCount": 1},
                                }
                            }
                        }
                    ]
                },
            ).execute()
        escaped = title.replace("'", "''")
        header_response = (
            self.service.spreadsheets()
            .values()
            .get(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{escaped}'!1:1",
            )
            .execute()
        )
        current = header_response.get("values", [])
        if not current:
            self.service.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{escaped}'!A1",
                valueInputOption="RAW",
                body={"values": [list(headers)]},
            ).execute()
        elif tuple(current[0][: len(headers)]) != headers:
            raise RuntimeError(f"Google Sheet {title!r} headers changed")
        return escaped

    def _bot_user_rows(self) -> tuple[str, list[list[str]]]:
        title = self._ensure_sheet(BOT_USERS_SHEET, BOT_USER_HEADERS)
        response = (
            self.service.spreadsheets()
            .values()
            .get(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{title}'!A:J",
            )
            .execute()
        )
        return title, response.get("values", [])

    def find_registry_user(self, telegram_username: str) -> RegistryMatch | None:
        with self._write_lock:
            _, bot_rows = self._bot_user_rows()
        bot_match = bot_registry_match_from_rows(
            bot_rows,
            telegram_username=telegram_username,
        )
        if bot_match is not None:
            return bot_match
        title = self._sheet_title().replace("'", "''")
        response = (
            self.service.spreadsheets()
            .values()
            .get(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{title}'!A:I",
            )
            .execute()
        )
        rows = response.get("values", [])
        return registry_match_from_sheet_values(rows, telegram_username)

    def find_registry_user_by_server_username(
        self, server_username: str
    ) -> RegistryMatch | None:
        with self._write_lock:
            _, bot_rows = self._bot_user_rows()
        bot_match = bot_registry_match_from_rows(
            bot_rows,
            server_username=server_username,
        )
        if bot_match is not None:
            return bot_match
        title = self._sheet_title().replace("'", "''")
        response = (
            self.service.spreadsheets()
            .values()
            .get(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{title}'!A:I",
            )
            .execute()
        )
        rows = response.get("values", [])
        return registry_match_by_server_username_from_sheet_values(
            rows, server_username
        )

    def claim_registry_user_id(
        self,
        telegram_username: str,
        telegram_user_id: int,
    ) -> RegistryMatch | None:
        with self._write_lock:
            title = self._sheet_title().replace("'", "''")
            response = (
                self.service.spreadsheets()
                .values()
                .get(
                    spreadsheetId=self.spreadsheet_id,
                    range=f"'{title}'!A:K",
                )
                .execute()
            )
            rows = response.get("values", [])
            match, row_numbers = registry_claim_plan_from_sheet_values(
                rows, telegram_username, telegram_user_id
            )
            needs_headers = not rows or _cell(rows[0], 8) != BOT_HEADERS[0]
            if needs_headers or row_numbers:
                timestamp = datetime.now(UTC).strftime("%d-%m-%Y %H:%M:%S UTC")
                data = []
                if needs_headers:
                    data.append(
                        {
                            "range": f"'{title}'!I1:K1",
                            "values": [list(BOT_HEADERS)],
                        }
                    )
                data.extend(
                    {
                        "range": f"'{title}'!I{row_number}:K{row_number}",
                        "values": [[
                            str(telegram_user_id),
                            "Импортирован в бот",
                            timestamp,
                        ]],
                    }
                    for row_number in row_numbers
                )
                self.service.spreadsheets().values().batchUpdate(
                    spreadsheetId=self.spreadsheet_id,
                    body={"valueInputOption": "RAW", "data": data},
                ).execute()
            return match

    def sync_snapshot(
        self,
        user_records: tuple[
            tuple[Application | Profile, str, tuple[str, ...]], ...
        ],
        request_records: tuple[tuple[str, Application, str], ...],
    ) -> None:
        with self._write_lock:
            users_title = self._ensure_sheet(BOT_USERS_SHEET, BOT_USER_HEADERS)
            requests_title = self._ensure_sheet(
                BOT_REQUESTS_SHEET,
                BOT_REQUEST_HEADERS,
            )
            user_values = [list(BOT_USER_HEADERS)] + [
                bot_user_values(subject, status, services)
                for subject, status, services in user_records
            ]
            request_values = [list(BOT_REQUEST_HEADERS)] + [
                bot_request_values(request_id, application, status)
                for request_id, application, status in request_records
            ]
            existing = (
                self.service.spreadsheets()
                .values()
                .batchGet(
                    spreadsheetId=self.spreadsheet_id,
                    ranges=[
                        f"'{users_title}'!A:J",
                        f"'{requests_title}'!A:J",
                    ],
                )
                .execute()
                .get("valueRanges", [])
            )
            existing_user_count = len(
                existing[0].get("values", []) if len(existing) > 0 else []
            )
            existing_request_count = len(
                existing[1].get("values", []) if len(existing) > 1 else []
            )
            user_values = pad_snapshot_values(
                user_values,
                existing_user_count,
                len(BOT_USER_HEADERS),
            )
            request_values = pad_snapshot_values(
                request_values,
                existing_request_count,
                len(BOT_REQUEST_HEADERS),
            )
            self.service.spreadsheets().values().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={
                    "valueInputOption": "RAW",
                    "data": [
                        {
                            "range": (
                                f"'{users_title}'!A1:J{len(user_values)}"
                            ),
                            "values": user_values,
                        },
                        {
                            "range": (
                                f"'{requests_title}'!A1:J{len(request_values)}"
                            ),
                            "values": request_values,
                        },
                    ],
                },
            ).execute()
