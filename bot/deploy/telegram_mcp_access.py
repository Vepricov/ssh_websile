#!/usr/bin/env python3
"""Owner-approved roles for a stable Telegram identity, with pinned existing users.

The caller sends JSON on stdin. Tokens never occur in argv or stdout. Deployment
loads the existing MCP service environment, including its local DB connection.
"""

import datetime
import json
import os
import re
import secrets
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


GITEA = Path(os.environ.get("GITEA_ROOT", "/home/shkodnik1917/brainlab-stack/gitea"))
#: Логин на сайте. Подчёркивание и точка законны и у Gitea, и у GitLab, а прежний
#: образец их не пускал: `mikhail_pypin` молча не получал учётки вовсе, потому что
#: `site_account` на непрошедшем образце возвращает пустоту без всякой жалобы.
#: Точка на конце запрещена: GitLab читает её как расширение пути.
LOGIN = re.compile(r"[a-z0-9][a-z0-9._-]{1,38}(?<!\.)")
#: Где человеку заводить вход: `gitea` или `gitlab`. Переключается одной строкой окружения,
#: потому что переезд — это смена сервера, а не смена кода бота.
SITE = os.environ.get("LAB_ACCESS_SITE", "gitea").strip().lower()




def site_account(login: str, full_name: str, email: str) -> dict:
    """Завести человеку учётную запись на сайте базы и вернуть вход.

    Доступ к базе и вход на сайт — разные системы: в базе человек опознаётся телеграмом и
    ключом, Gitea про телеграм не знает вовсе. Прежде учётку заводил владелец руками, и
    участник с ключом неделями не мог открыть ни одной страницы. Владелец 30-09-2026:
    «пусть тоже создаёт и отправляет».

    Почта берётся та же, что у человека в базе: это мост между двумя личностями, а не
    второй аккаунт. Пароль временный, и Gitea потребует сменить его при первом входе.
    """
    if SITE == "gitlab":
        return gitlab_account(login, full_name, email)
    if not LOGIN.fullmatch(login):
        return {}
    present = subprocess.run(
        [str(GITEA / "bin" / "gitea"), "admin", "user", "list"],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "GITEA_WORK_DIR": str(GITEA)})
    for line_text in present.stdout.splitlines()[1:]:
        fields = line_text.split()
        if len(fields) > 1 and fields[1] == login:
            return {"gitea_login": login, "gitea_password": "",
                    "site_login": login, "site_password": ""}
    password = secrets.token_urlsafe(12)
    creation = subprocess.run(
        [str(GITEA / "bin" / "gitea"), "admin", "user", "create",
         "--username", login, "--email", email, "--password", password,
         "--fullname", full_name or login, "--must-change-password"],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "GITEA_WORK_DIR": str(GITEA)})
    if creation.returncode:
        return {}
    return {"gitea_login": login, "gitea_password": password,
            "site_login": login, "site_password": password,
            **git_key(login), **to_readers(login)}


#: Петля, на которой GitLab слушает на сервере базы. Наружу он ходит через Caddy.
GITLAB_DEFAULT = "http://127.0.0.1:9080"


def _gitlab(path: str, *, method: str = "GET", payload: dict | None = None):
    address = (os.environ.get("LAB_GITLAB_URL") or GITLAB_DEFAULT).rstrip("/")
    token = os.environ.get("LAB_GITLAB_TOKEN", "")
    if not token:
        raise RuntimeError("GitLab не настроен: нет LAB_GITLAB_TOKEN")
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{address}/api/v4/{path}", data=body, method=method,
        headers={"PRIVATE-TOKEN": token, "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as answer:
        raw = answer.read().decode("utf-8")
    return json.loads(raw) if raw.strip() else None


def gitlab_account(login: str, full_name: str, email: str) -> dict:
    """Завести человеку учётку в GitLab, ключ для git и роль разработчика в лаборатории.

    Команды читателей здесь нет и не нужно: дерево лаборатории видно любому, кто вошёл
    (видимость `internal`), а регистрация закрыта. То, что в Gitea держалось списком
    членов и расходилось с правдой, в GitLab держится одним свойством группы.

    Ключ для git выдаётся всегда, в том числе при повторной заявке: без него участник
    должен искать Settings → Access tokens сам, и это первый шаг, на котором люди
    застревают. Имя ключа уникальное, иначе повторная выдача молча ничего не делает.
    """
    if not LOGIN.fullmatch(login):
        return {}
    address = (os.environ.get("LAB_GITLAB_PUBLIC_URL")
             or os.environ.get("LAB_GITLAB_URL") or GITLAB_DEFAULT).rstrip("/")
    present = _gitlab(f"users?username={urllib.parse.quote(login)}") or []
    password = ""
    if present:
        account = present[0]
        # Повторная выдача оставляла человека с логином и БЕЗ пароля, а почта на сервере
        # выключена, значит «забыли пароль» не работает тоже. Выдача повторяется по
        # построению — бот сам просит нажать «Одобрить» ещё раз, — и после первой
        # неудачи вход был недостижим. 04-10-2026 так встали две заявки.
        #
        # Пароль перевыдаётся ТОЛЬКО тому, кто ни разу не входил: тогда прежним никто не
        # пользуется, и замена ничего не ломает. Человеку, который уже входил, пароль не
        # трогаем — он у него есть, и менять его за его спиной нельзя.
        if not (account.get("current_sign_in_at") or account.get("last_sign_in_at")):
            password = secrets.token_urlsafe(12)
            try:
                _gitlab(f"users/{account['id']}", method="PUT",
                        payload={"password": password})
            except (urllib.error.HTTPError, urllib.error.URLError, OSError):
                # Не вышло — человек получит сообщение без строки про пароль, но ключ
                # для git и логин он получит: одно не должно отменять другое.
                password = ""
    else:
        password = secrets.token_urlsafe(12)
        account = _gitlab("users", method="POST", payload={
            "email": email, "username": login, "name": full_name or login,
            "password": password, "skip_confirmation": True,
            # Своих групп верхнего уровня не заводим: всё живёт в дереве лаборатории.
            "can_create_group": False})
    due = (datetime.date.today() + datetime.timedelta(days=365)).isoformat()
    key_name = _gitlab(f"users/{account['id']}/personal_access_tokens", method="POST", payload={
        "name": f"git-{secrets.token_hex(3)}", "expires_at": due,
        "scopes": ["api", "write_repository"]}) or {}
    # В КОРНЕВУЮ ГРУППУ НЕ ЗАПИСЫВАЕМ. Членство в GitLab наследуется вниз, и разработчик
    # корня — это разработчик каждой работы лаборатории сразу: право пушить ветки во все
    # чужие работы, выданное за факт входа. Человек приходит пустым: дерево и литературу
    # ему даёт видимость `internal`, а доступ к работе — отдельная заявка на её тему,
    # которую решает ведущий. Уровень ниже унаследованного GitLab поставить не даёт,
    # поэтому лишнее членство в корне ещё и заперло бы роль читателя на теме.
    out = {"site_login": login, "site_password": password, "site_url": address,
             # Номер учётки: им бот отвечает на вопрос «кому выдано», и прежде он
             # приходил из базы службы MCP, которой больше нет.
             "site_user_id": str(account["id"]),
             # Прежние имена: бот умеет и их, пока не обновлён.
             "gitea_login": login, "gitea_password": password}
    if key_name.get("token"):
        out["git_token"] = key_name["token"]
    return out


def to_readers(login: str) -> dict:
    """Дать человеку чтение всех тем, кроме грантов.

    Без членства в организации приватный репозиторий отвечает «not found» на верном ключе,
    и человек с ключом и учёткой всё равно не может склонировать ничего. Команда
    `readers` собрана из всех тем, кроме `grants`: гранты видят только причастные.

    Право на запись этим не выдаётся: предлагать правки в работу можно, только будучи её
    участником, и это решение ведущего работы.
    """
    token = os.environ.get("LAB_KNOWLEDGE_GIT_OWNER_TOKEN", "")
    address = os.environ.get("LAB_KNOWLEDGE_GIT_PUBLIC_URL") or os.environ.get(
        "LAB_KNOWLEDGE_GIT_URL", "http://127.0.0.1:3080")
    if not token:
        return {}
    request = urllib.request.Request(
        f"{address.rstrip('/')}/api/v1/teams/54/members/{login}", method="PUT",
        headers={"Authorization": f"token {token}"})
    try:
        with urllib.request.urlopen(request, timeout=30) as answer:
            answer.read()
    except urllib.error.HTTPError:
        return {}
    return {"gitea_team": "readers"}


def git_key(login: str) -> dict:
    """Выдать человеку ключ Gitea, которым его агент ходит в git по HTTPS.

    Без него участник должен сам найти в интерфейсе Settings → Applications и завести
    токен — лишний шаг на пустом месте, и первый, на котором люди застревают. Права
    узкие: читать и писать репозитории и обсуждения, не больше.
    """
    grant = subprocess.run(
        [str(GITEA / "bin" / "gitea"), "admin", "user", "generate-access-token",
         # Имя ключа уникальное: Gitea отказывается заводить второй с тем же именем,
         # и повторная выдача молча возвращала пустоту.
         "--username", login, "--token-name", f"git-{secrets.token_hex(3)}", "--raw",
         "--scopes", "write:repository,write:issue"],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "GITEA_WORK_DIR": str(GITEA)})
    key_name = grant.stdout.strip().splitlines()[-1].strip() if grant.stdout.strip() else ""
    if grant.returncode or not re.fullmatch(r"[0-9a-f]{40}", key_name):
        return {}
    return {"git_token": key_name}


def gitlab_user(email: str) -> dict:
    """Найти учётку по подставной почте, которой её завели."""
    found = _gitlab(f"users?search={urllib.parse.quote(email)}") or []
    for account in found:
        if (account.get("email") or "").lower() == email.lower():
            return account
    return {}


def revoke_keys(account: dict) -> int:
    """Отозвать ключи git, выданные нами: имя начинается с `git-`.

    Учётку не блокируем. «Отозвать ключ» в боте означает, что агент теряет доступ к
    репозиториям, а не что человек теряет вход: вход у него свой, и отбирать его за
    отзыв ключа никто не просил.
    """
    keys = _gitlab(f"personal_access_tokens?user_id={account['id']}") or []
    gone = 0
    for key in keys:
        if not str(key.get("name", "")).startswith("git-") or key.get("revoked"):
            continue
        try:
            _gitlab(f"personal_access_tokens/{key['id']}", method="DELETE")
        except (urllib.error.HTTPError, urllib.error.URLError, OSError):
            continue
        gone += 1
    return gone


def grant(payload: dict) -> dict:
    """Одна заявка: завести человеку вход в базу или отозвать его ключи.

    Прежде этот ход шёл через службу MCP: она держала свою базу людей, ключей и ролей, и
    вход на сайт был у неё побочным делом. Службу вместе с её базой удалили 07-10-2026, и
    с этого дня каждое нажатие «Одобрить» падало с `ModuleNotFoundError: grant_mcp_access`
    — то есть человек, подавший заявку, не получал ничего, а владелец видел «Выдача или
    доставка не завершена» и нажимал снова. Теперь выдача делает ровно то, что нужно: вход
    в GitLab, пароль на первый раз и ключ для git.

    Право решать проверяет бот (`service.admins`), и до сюда доходит только одобренная
    заявка: скрипт запускается по ssh учёткой сервера базы и наружу не открыт.
    """
    allowed = {"action", "telegram_user_id", "token", "role", "username", "full_name"}
    if not {"action", "telegram_user_id"} <= set(payload) <= allowed:
        raise ValueError("Unexpected request fields")
    user_id = payload["telegram_user_id"]
    if type(user_id) is not int or not 0 < user_id < 2**52:
        raise ValueError("Invalid Telegram ID")
    action = payload["action"]
    if action not in {"issue", "revoke"}:
        raise ValueError("Unsupported action")
    # Ключ MCP больше ничего не открывает, но бот его по-прежнему присылает. Принимаем и
    # не используем: ломать бота ради поля, которое он скоро перестанет посылать, не надо.
    token = payload.get("token", "")
    if not isinstance(token, str) or (token and not re.fullmatch(r"[0-9a-f]{64}", token)):
        raise ValueError("Invalid token")
    role = str(payload.get("role") or "member")
    if role not in {"member", "manager", "lead"}:
        raise ValueError("Invalid role")
    email = f"telegram-{user_id}@users.brainlab-ai.com"

    if action == "revoke":
        account = gitlab_user(email)
        if not account:
            raise ValueError("Учётки с таким телеграмом нет")
        return {"email": email, "role": role, "user_id": str(account["id"]),
                "telegram_user_id": user_id, "revoked": revoke_keys(account)}

    wanted = re.sub(r"[^a-z0-9-]", "-", str(payload.get("username") or "").strip().lower()).strip("-")
    entry = site_account(wanted or f"tg{user_id}", str(payload.get("full_name") or ""), email)
    if not entry.get("site_login") or not entry.get("site_user_id"):
        # Прежде вход был побочным делом, и его отсутствие сообщение не отменяло. Теперь
        # он и есть выдача: молча отдать заявку без входа значит соврать обоим.
        raise RuntimeError("Вход в базу не создан: проверь LAB_GITLAB_TOKEN и свободен ли логин")
    return {"email": email, "role": role, "user_id": entry["site_user_id"],
            "telegram_user_id": user_id, **entry}


def main() -> int:
    try:
        raw = sys.stdin.read(4097)
        if len(raw) > 4096:
            raise ValueError("Request too large")
        result = grant(json.loads(raw))
    except Exception as error:
        # Причина отказа печатается, а полезная нагрузка — нет. Прежде здесь было одно
        # имя исключения, и бот показывал человеку «Выдача не завершена», а в журнале
        # лежало `RuntimeError` без слова о том, что случилось. 04-10-2026 на этом встали
        # две заявки, и разбираться пришлось запуском скрипта руками.
        print(f"{type(error).__name__}: {str(error)[:200]}", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
