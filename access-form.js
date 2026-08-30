(() => {
    "use strict";

    const telegram = window.Telegram?.WebApp;
    const form = document.getElementById("access-form");
    const submitButton = document.getElementById("submit-button");
    const submitLabel = submitButton.querySelector("span");
    const formError = document.getElementById("form-error");
    const fullName = document.getElementById("full-name");
    const recommender = document.getElementById("recommender");
    let submitted = false;

    telegram?.ready();
    telegram?.expand();

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

    function validateForm() {
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

    form.addEventListener("input", (event) => {
        event.target.setCustomValidity?.("");
        formError.hidden = true;
    });

    form.addEventListener("submit", (event) => {
        event.preventDefault();
        if (submitted || !validateForm()) {
            return;
        }
        const data = JSON.stringify({
            version: 1,
            kind: "profile",
            full_name: fullName.value.trim(),
            recommender: recommender.value.trim(),
        });
        if (!telegram?.sendData) {
            formError.textContent = "Откройте эту форму кнопкой внутри Telegram-бота.";
            formError.hidden = false;
            return;
        }
        submitted = true;
        submitButton.disabled = true;
        submitLabel.textContent = "Сохраняю…";
        telegram.sendData(data);
    });
})();
