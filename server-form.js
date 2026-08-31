(() => {
    "use strict";

    const telegram = window.Telegram?.WebApp;
    const cloudStorage = telegram?.isVersionAtLeast?.("6.9")
        ? telegram.CloudStorage
        : null;
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
    const serverUsername = document.getElementById("server-username");
    const storageKey = "brainlab_server_request_v1";
    let submitted = false;
    let touched = false;
    let saveTimer;

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

    function loadDraft() {
        const finish = (rawDraft) => {
            let draft = null;
            try {
                draft = JSON.parse(rawDraft || "null");
            } catch {
                // Ignore a malformed cloud draft.
            }
            if (touched) {
                applyRequestedServer();
                return;
            }
            if (!draft) {
                applyRequestedServer();
                return;
            }
            if (typeof draft.server_username === "string") {
                serverUsername.value = draft.server_username;
            }
            if (typeof draft.activity === "string") {
                activity.value = draft.activity;
                activityCount.textContent = String(activity.value.length);
            }
            if (draft.access_term === "бессрочно") {
                indefinite.checked = true;
                accessDate.disabled = true;
            } else if (
                typeof draft.access_term === "string"
                && draft.access_term >= todayIso
            ) {
                accessDate.value = draft.access_term;
            }
            if (Array.isArray(draft.servers)) {
                for (const input of form.querySelectorAll('input[name="servers"]')) {
                    input.checked = draft.servers.includes(input.value);
                }
            }
            applyRequestedServer();
        };
        if (!cloudStorage?.getItem) {
            finish(null);
            return;
        }
        cloudStorage.getItem(storageKey, (error, value) => {
            finish(error ? null : value);
        });
    }

    function saveDraft() {
        if (!cloudStorage?.setItem) {
            return;
        }
        cloudStorage.setItem(
            storageKey,
            JSON.stringify({
                server_username: serverUsername.value,
                access_term: indefinite.checked ? "бессрочно" : accessDate.value,
                activity: activity.value,
                servers: selectedServers(),
            }),
        );
    }

    function queueDraftSave() {
        window.clearTimeout(saveTimer);
        saveTimer = window.setTimeout(saveDraft, 250);
    }

    const requestedServer = new URLSearchParams(window.location.search).get("server");
    function applyRequestedServer() {
        const requestedCheckbox = Array.from(
            form.querySelectorAll('input[name="servers"]'),
        ).find((input) => input.value === requestedServer);
        if (requestedCheckbox) {
            requestedCheckbox.checked = true;
        }
    }

    loadDraft();

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
        } else if (
            payload.servers.includes("mlsub")
            && payload.servers.length > 1
        ) {
            setError(
                serversError,
                "mlsub оформляется отдельной заявкой. Оставьте выбранным только mlsub.",
            );
            valid = false;
        }
        return valid;
    }

    indefinite.addEventListener("change", () => {
        touched = true;
        accessDate.disabled = indefinite.checked;
        if (indefinite.checked) {
            accessDate.value = "";
            setError(termError, "");
        }
        queueDraftSave();
    });

    activity.addEventListener("input", () => {
        activityCount.textContent = String(activity.value.length);
    });

    form.addEventListener("input", () => {
        touched = true;
        formError.hidden = true;
        queueDraftSave();
    });

    form.addEventListener("change", () => {
        touched = true;
        queueDraftSave();
    });

    form.addEventListener("submit", (event) => {
        event.preventDefault();
        if (submitted || !form.reportValidity()) {
            return;
        }
        const payload = {
            version: 1,
            kind: "server_request",
            server_username: serverUsername.value.trim(),
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
        window.clearTimeout(saveTimer);
        saveDraft();
        submitted = true;
        submitButton.disabled = true;
        submitLabel.textContent = "Отправляю…";
        telegram.sendData(data);
    });
})();
