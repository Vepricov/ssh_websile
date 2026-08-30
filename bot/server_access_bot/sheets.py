from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from .domain import Application, RegistryMatch

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
BOT_HEADERS = ("Telegram user ID", "Статус выдачи", "Обновлено ботом")
SERVER_LABELS = {
    "shkodnik_opt": "Старый сервер (MIPT) -- x4 RTX 2080 11гб, x2 A100 40гб, x2 A100 80гб",
    "brain_lab": "Новый сервер (BrainLab) -- x4 A100 80гб",
    "vv_h200": "H200 (VB) - x8 H200 140гб",
    "mlsub": "mlsub / Cloud.ru — отдельный сервис через BrainLab",
}
REGISTRY_SERVERS = (
    ("shkodnik_opt", ("Старый сервер (MIPT)", "Старый (mipt)")),
    ("brain_lab", ("Новый сервер (BrainLab)", "Новый (BrainLab)")),
    ("vv_h200", ("H200",)),
    ("yandex", ("Яндекс",)),
    ("mlsub", ("mlsub / Cloud.ru",)),
)


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
    identities = {
        (
            " ".join(_cell(row, 1).split()).casefold(),
            _cell(row, 2).lower(),
        )
        for row in matched
    }
    if len(identities) != 1:
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
        server_username=_cell(latest, 2),
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


def application_values(
    application: Application,
    status: str,
    now: datetime | None = None,
) -> list[str]:
    timestamp = (now or datetime.now(UTC)).strftime("%d-%m-%Y %H:%M:%S UTC")
    telegram_username = application.telegram_username or ""
    if telegram_username and not telegram_username.startswith("@"):
        telegram_username = "@" + telegram_username
    return [
        timestamp,
        application.full_name,
        application.server_username,
        application.access_term,
        application.recommender,
        telegram_username,
        ", ".join(SERVER_LABELS[host] for host in application.servers),
        application.activity,
        str(application.telegram_user_id),
        status,
        timestamp,
    ]


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

    def find_registry_user(self, telegram_username: str) -> RegistryMatch | None:
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

    def upsert(self, application: Application, status: str) -> None:
        title = self._sheet_title().replace("'", "''")
        sheet_range = f"'{title}'!A:K"
        response = (
            self.service.spreadsheets()
            .values()
            .get(spreadsheetId=self.spreadsheet_id, range=sheet_range)
            .execute()
        )
        rows = response.get("values", [])
        if not rows:
            raise RuntimeError("Google Sheet has no header row")
        header = list(rows[0]) + [""] * max(0, 11 - len(rows[0]))
        if tuple(header[:8]) != FORM_HEADERS:
            raise RuntimeError("Google Sheet form headers changed")
        if tuple(header[8:11]) != BOT_HEADERS:
            (
                self.service.spreadsheets()
                .values()
                .update(
                    spreadsheetId=self.spreadsheet_id,
                    range=f"'{title}'!I1:K1",
                    valueInputOption="RAW",
                    body={"values": [list(BOT_HEADERS)]},
                )
                .execute()
            )
        values = application_values(application, status)
        target_row = next(
            (
                index
                for index, row in enumerate(rows[1:], start=2)
                if len(row) > 8 and row[8] == str(application.telegram_user_id)
            ),
            None,
        )
        api = self.service.spreadsheets().values()
        if target_row is None:
            api.append(
                spreadsheetId=self.spreadsheet_id,
                range=sheet_range,
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": [values]},
            ).execute()
        else:
            api.update(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{title}'!A{target_row}:K{target_row}",
                valueInputOption="RAW",
                body={"values": [values]},
            ).execute()
