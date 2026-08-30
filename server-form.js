(() => {
    "use strict";

    const telegram = window.Telegram?.WebApp;
    const form = document.getElementById("server-form");
    const submitButton = document.getElementById("submit-button");
    const submitLabel = submitButton.querySelector("span");
    const accessDate = document.getElementById("access-date");
    const indefinite = document.getElementById("indefinite");
    const termError = document.getElementById("term-error");
    const serversError = document.getElementById("servers-error");
    const formError = document.getElementById("form-error");
    const activity = document.getElementById("activity");
    const activityCount = document.getElementById("activity-count");
    let submitted = false;

    telegram?.ready();
    telegram?.expand();

    const moscowDateParts = Object.fromEntries(
        new Intl.DateTimeFormat("en-US", {
            timeZone: "Europe/Moscow",
            year: "numeric",
            month: "2-digit",
            day: "2-digit",
        })
            .formatToParts(new Date())
            .filter((part) => part.type !== "literal")
            .map((part) => [part.type, part.value]),
    );
    const todayIso = `${moscowDateParts.year}-${moscowDateParts.month}-${moscowDateParts.day}`;
    accessDate.min = todayIso;

    const requestedServer = new URLSearchParams(window.location.search).get("server");
    const requestedCheckbox = Array.from(
        form.querySelectorAll('input[name="servers"]'),
    ).find((input) => input.value === requestedServer);
    if (requestedCheckbox) {
        requestedCheckbox.checked = true;
    }

    function selectedServers() {
        return Array.from(form.querySelectorAll('input[name="servers"]:checked'))
            .map((input) => input.value);
    }

    function setError(element, message) {
        element.textContent = message;
    }

    function validateSelection(payload) {
        let valid = true;
        setError(termError, "");
        setError(serversError, "");
        formError.hidden = true;
        if (!payload.access_term) {
            setError(termError, "Укажите дату окончания или включите «Бессрочно».");
            valid = false;
        } else if (payload.access_term !== "бессрочно" && payload.access_term < todayIso) {
            setError(termError, "Дата окончания не может быть в прошлом.");
            valid = false;
        }
        if (payload.servers.length === 0) {
            setError(serversError, "Выберите хотя бы один сервер.");
            valid = false;
        }
        return valid;
    }

    indefinite.addEventListener("change", () => {
        accessDate.disabled = indefinite.checked;
        if (indefinite.checked) {
            accessDate.value = "";
            setError(termError, "");
        }
    });

    activity.addEventListener("input", () => {
        activityCount.textContent = String(activity.value.length);
    });

    form.addEventListener("input", () => {
        formError.hidden = true;
    });

    form.addEventListener("submit", (event) => {
        event.preventDefault();
        if (submitted || !form.reportValidity()) {
            return;
        }
        const payload = {
            version: 1,
            kind: "server_request",
            server_username: document.getElementById("server-username").value.trim(),
            access_term: indefinite.checked ? "бессрочно" : accessDate.value,
            activity: activity.value.trim(),
            servers: selectedServers(),
        };
        if (!validateSelection(payload)) {
            return;
        }
        const data = JSON.stringify(payload);
        if (new TextEncoder().encode(data).length > 4096) {
            formError.textContent = "Ответы не помещаются в сообщение Telegram. Сократите описание задач.";
            formError.hidden = false;
            return;
        }
        if (!telegram?.sendData) {
            formError.textContent = "Откройте эту форму кнопкой внутри Telegram-бота.";
            formError.hidden = false;
            return;
        }
        submitted = true;
        submitButton.disabled = true;
        submitLabel.textContent = "Отправляю…";
        telegram.sendData(data);
    });
})();
