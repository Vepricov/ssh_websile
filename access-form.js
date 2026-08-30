(() => {
    "use strict";

    const telegram = window.Telegram?.WebApp;
    const form = document.getElementById("access-form");
    const submitButton = document.getElementById("submit-button");
    const submitLabel = submitButton.querySelector("span");
    const review = document.getElementById("review");
    const reviewList = document.getElementById("review-list");
    const editButton = document.getElementById("edit-button");
    const accessDate = document.getElementById("access-date");
    const indefinite = document.getElementById("indefinite");
    const termError = document.getElementById("term-error");
    const serversError = document.getElementById("servers-error");
    const formError = document.getElementById("form-error");
    const fullName = document.getElementById("full-name");
    const recommender = document.getElementById("recommender");
    const activity = document.getElementById("activity");
    const activityCount = document.getElementById("activity-count");
    let reviewedPayload = null;
    let submitted = false;

    const serverLabels = {
        shkodnik_opt: "Старый сервер (MIPT)",
        brain_lab: "Новый сервер (BrainLab)",
        vv_h200: "H200 (VV)",
    };

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

    function isHumanName(value) {
        const words = value.trim().split(/\s+/);
        return words.length >= 2
            && words.every((word) => /^(?=.*\p{L})[\p{L}-]+$/u.test(word));
    }

    function isTelegramHandle(value) {
        return /^@[A-Za-z0-9_]{3,}$/.test(value);
    }

    function isRecommender(value) {
        const normalized = value.trim();
        if (isTelegramHandle(normalized) || isHumanName(normalized)) {
            return true;
        }
        const separator = normalized.lastIndexOf(" ");
        return separator > 0
            && isHumanName(normalized.slice(0, separator))
            && isTelegramHandle(normalized.slice(separator + 1));
    }

    function validateTextFields() {
        fullName.setCustomValidity(
            isHumanName(fullName.value) ? "" : "Укажите имя и фамилию.",
        );
        recommender.setCustomValidity(
            isRecommender(recommender.value)
                ? ""
                : "Укажите имя и фамилию или Telegram-ник рекомендателя.",
        );
        return form.reportValidity();
    }

    function selectedServers() {
        return Array.from(form.querySelectorAll('input[name="servers"]:checked'))
            .map((input) => input.value);
    }

    function payloadFromForm() {
        return {
            version: 1,
            full_name: document.getElementById("full-name").value.trim(),
            server_username: document.getElementById("server-username").value.trim(),
            access_term: indefinite.checked ? "бессрочно" : accessDate.value,
            recommender: document.getElementById("recommender").value.trim(),
            activity: activity.value.trim(),
            servers: selectedServers(),
        };
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

    function addReviewRow(label, value) {
        const term = document.createElement("dt");
        const description = document.createElement("dd");
        term.textContent = label;
        description.textContent = value || "Не указано";
        reviewList.append(term, description);
    }

    function showReview(payload) {
        reviewList.replaceChildren();
        addReviewRow("Имя", payload.full_name);
        addReviewRow("Ник", payload.server_username);
        addReviewRow("Срок", payload.access_term);
        addReviewRow("Рекомендатель", payload.recommender);
        addReviewRow("Серверы", payload.servers.map((host) => serverLabels[host]).join(", "));
        addReviewRow("Задачи", payload.activity);
        review.hidden = false;
        submitLabel.textContent = "Отправить боту";
        review.scrollIntoView({ behavior: "smooth", block: "start" });
    }

    function resetReview() {
        reviewedPayload = null;
        review.hidden = true;
        submitLabel.textContent = "Проверить данные";
        formError.hidden = true;
    }

    indefinite.addEventListener("change", () => {
        accessDate.disabled = indefinite.checked;
        if (indefinite.checked) {
            accessDate.value = "";
            setError(termError, "");
        }
        resetReview();
    });

    activity.addEventListener("input", () => {
        activityCount.textContent = String(activity.value.length);
    });

    form.addEventListener("input", (event) => {
        if (event.target === fullName || event.target === recommender) {
            event.target.setCustomValidity("");
        }
        resetReview();
    });

    form.addEventListener("change", () => {
        resetReview();
    });

    editButton.addEventListener("click", () => {
        resetReview();
        form.querySelector("input, textarea")?.focus();
    });

    form.addEventListener("submit", (event) => {
        event.preventDefault();
        if (submitted) {
            return;
        }
        if (!validateTextFields()) {
            return;
        }

        const payload = payloadFromForm();
        if (!validateSelection(payload)) {
            return;
        }

        if (reviewedPayload === null) {
            reviewedPayload = payload;
            showReview(payload);
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
