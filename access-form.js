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
    const recommenderGrid = document.getElementById("recommender-grid");
    const recommenderEmpty = document.getElementById("recommender-empty");
    const storageKey = "brainlab_server_access_profile_v1";
    let submitted = false;
    let touched = false;
    let saveTimer;

    telegram?.ready();
    telegram?.expand();

    const candidates = parseCandidates();
    renderRecommenders(candidates);

    function parseCandidates() {
        const raw = new URLSearchParams(window.location.search).get("rec");
        let parsed = null;
        try {
            parsed = JSON.parse(raw || "[]");
        } catch {
            return [];
        }
        if (!Array.isArray(parsed)) {
            return [];
        }
        return parsed.filter(
            (item) =>
                item
                && Number.isInteger(item.id)
                && item.id > 0
                && typeof item.label === "string",
        );
    }

    function initials(label) {
        const name = label.replace(/\(@[^)]*\)/g, "").trim() || label;
        const words = name.replace(/^@/, "").split(/\s+/).filter(Boolean);
        return words.slice(0, 2).map((word) => word[0].toUpperCase()).join("");
    }

    function renderRecommenders(items) {
        if (!items.length) {
            recommenderGrid.hidden = true;
            recommenderEmpty.hidden = false;
            return;
        }
        for (const item of items) {
            const card = document.createElement("label");
            card.className = "server-card";

            const input = document.createElement("input");
            input.type = "checkbox";
            input.name = "recommender";
            input.value = String(item.id);

            const check = document.createElement("span");
            check.className = "server-check";
            check.setAttribute("aria-hidden", "true");
            check.textContent = "✓";

            const kicker = document.createElement("span");
            kicker.className = "server-kicker";
            kicker.textContent = initials(item.label) || "·";

            const name = document.createElement("b");
            name.textContent = item.label;

            card.append(input, check, kicker, name);
            recommenderGrid.append(card);
        }
    }

    function selectedRecommenders() {
        const checked = Array.from(
            recommenderGrid.querySelectorAll('input[name="recommender"]:checked'),
        );
        const ids = checked.map((input) => Number(input.value));
        const labels = checked.map(
            (input) =>
                candidates.find((item) => item.id === Number(input.value))?.label
                || "",
        );
        return { ids, labels };
    }

    function applySelectedIds(ids) {
        const wanted = new Set((ids || []).map(Number));
        for (const input of recommenderGrid.querySelectorAll(
            'input[name="recommender"]',
        )) {
            input.checked = wanted.has(Number(input.value));
        }
    }

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
            if (draft && Array.isArray(draft.recommender_ids)) {
                applySelectedIds(draft.recommender_ids);
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
        const selection = selectedRecommenders();
        cloudStorage.setItem(
            storageKey,
            JSON.stringify({
                full_name: fullName.value,
                recommender: selection.labels.join(", "),
                recommender_ids: selection.ids,
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

    form.addEventListener("change", () => {
        touched = true;
        queueDraftSave();
    });

    form.addEventListener("submit", (event) => {
        event.preventDefault();
        if (submitted || !validateForm()) {
            return;
        }
        const selection = selectedRecommenders();
        const data = JSON.stringify({
            version: 1,
            kind: "profile",
            full_name: fullName.value.trim(),
            recommender: selection.labels.join(", "),
            recommender_ids: selection.ids,
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
