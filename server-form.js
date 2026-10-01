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
    const urlParameters = new URLSearchParams(window.location.search);
    const requestedServer = urlParameters.get("server");
    const savedServerUsername = urlParameters.get("username");
    const existingServers = new Set(
        (urlParameters.get("existing") || "")
            .split(",")
            .filter(Boolean),
    );
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
                applyBotContext();
                return;
            }
            if (!draft) {
                applyBotContext();
                return;
            }
            if (typeof draft.server_username === "string") {
                serverUsername.value = draft.server_username.toLowerCase();
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
            applyBotContext();
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

    function applyBotContext() {
        if (
            savedServerUsername
            && /^[a-z_][a-z0-9_-]{0,31}$/.test(savedServerUsername)
        ) {
            serverUsername.value = savedServerUsername;
        }
        for (const input of form.querySelectorAll('input[name="servers"]')) {
            if (!existingServers.has(input.value)) {
                continue;
            }
            input.checked = true;
            input.disabled = true;
            input.closest(".server-card")?.setAttribute("aria-disabled", "true");
        }
        const requestedCheckbox = Array.from(
            form.querySelectorAll('input[name="servers"]'),
        ).find((input) => input.value === requestedServer);
        if (requestedCheckbox && !requestedCheckbox.disabled) {
            requestedCheckbox.checked = true;
        }
    }

    loadDraft();

    function selectedServers() {
        return Array.from(
            form.querySelectorAll('input[name="servers"]:checked:not(:disabled)'),
        )
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
        const servers = payload.servers;
        if (!payload.access_term) {
            setError(termError, "Укажите дату окончания или включите «Бессрочно».");
            valid = false;
        } else if (payload.access_term !== "бессрочно" && payload.access_term < todayIso) {
            setError(termError, "Дата окончания не может быть в прошлом.");
            valid = false;
        }
        if (servers.length === 0) {
            setError(serversError, "Выберите хотя бы один сервер.");
            valid = false;
        } else if (
            servers.includes("mlsub")
            && servers.length > 1
        ) {
            setError(
                serversError,
                "mlsub оформляется отдельной заявкой. Выберите mlsub без других серверов.",
            );
            valid = false;
        } else if (
            servers.includes("alibaba_ascend")
            && !/^[a-z][a-z0-9_-]{1,30}$/.test(payload.server_username)
        ) {
            serverUsername.setCustomValidity(
                "Для Alibaba ник должен начинаться с латинской буквы и содержать от 2 до 31 символа.",
            );
            serverUsername.reportValidity();
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

    serverUsername.addEventListener("input", () => {
        const lowered = serverUsername.value.toLowerCase();
        if (lowered !== serverUsername.value) {
            const caret = serverUsername.selectionStart;
            serverUsername.value = lowered;
            serverUsername.setSelectionRange(caret, caret);
        }
    });

    form.addEventListener("input", () => {
        touched = true;
        serverUsername.setCustomValidity("");
        formError.hidden = true;
        queueDraftSave();
    });

    form.addEventListener("change", () => {
        touched = true;
        serverUsername.setCustomValidity("");
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
            server_username: serverUsername.value.trim().toLowerCase(),
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
