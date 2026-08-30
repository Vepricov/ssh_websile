from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from .domain import Application

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
    "shkodnik_opt": "Старый (mipt) -- x4 RTX 2080 11гб, x2 A100 40гб, x2 A100 80гб",
    "brain_lab": "Новый (BrainLab) -- x4 A100 80гб",
    "vv_h200": "H200 (VB) - x8 H200 140гб",
}


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
