(() => {
    "use strict";

    const telegram = window.Telegram?.WebApp;
    const cloudStorage = telegram?.isVersionAtLeast?.("6.9")
        ? telegram.CloudStorage
        : null;
    const form = document.getElementById("access-form");
    const submitButton = document.getElementById("submit-button");
    const submitLabel = submitButton.querySelector("span");
    const formError = document.getElementById("form-error");
    const fullName = document.getElementById("full-name");
    const recommender = document.getElementById("recommender");
    const storageKey = "brainlab_server_access_profile_v1";
    let submitted = false;
    let touched = false;
    let saveTimer;

    telegram?.ready();
    telegram?.expand();

    function loadDraft() {
        const finish = (rawDraft) => {
            let draft = null;
            try {
                draft = JSON.parse(rawDraft || "null");
            } catch {
                // Ignore a malformed cloud draft and use Telegram profile data.
            }
            if (touched) {
                return;
            }
            if (draft && typeof draft.full_name === "string") {
                fullName.value = draft.full_name;
            }
            if (draft && typeof draft.recommender === "string") {
                recommender.value = draft.recommender;
            }
            if (!fullName.value) {
                const user = telegram?.initDataUnsafe?.user;
                fullName.value = [user?.first_name, user?.last_name]
                    .filter(Boolean)
                    .join(" ");
            }
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
                full_name: fullName.value,
                recommender: recommender.value,
            }),
        );
    }

    function queueDraftSave() {
        window.clearTimeout(saveTimer);
        saveTimer = window.setTimeout(saveDraft, 250);
    }

    loadDraft();

    function isHumanName(value) {
        const words = value.trim().split(/\s+/);
        return words.length >= 2
            && words.every((word) => /^(?=.*\p{L})[\p{L}-]+$/u.test(word));
    }

    function validateForm() {
        fullName.setCustomValidity(
            isHumanName(fullName.value) ? "" : "Укажите имя и фамилию.",
        );
        return form.reportValidity();
    }

    form.addEventListener("input", (event) => {
        touched = true;
        event.target.setCustomValidity?.("");
        formError.hidden = true;
        queueDraftSave();
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
        window.clearTimeout(saveTimer);
        saveDraft();
        submitted = true;
        submitButton.disabled = true;
        submitLabel.textContent = "Сохраняю…";
        telegram.sendData(data);
    });
})();
