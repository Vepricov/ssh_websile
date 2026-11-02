(() => {
    "use strict";

    const telegram = window.Telegram?.WebApp;
    const cloud = telegram?.isVersionAtLeast?.("6.9") ? telegram.CloudStorage : null;
    const form = document.getElementById("server-form");
    const fields = Object.fromEntries(["full-name", "username", "activity"]
        .map((id) => [id, document.getElementById(id)]));
    const recommenderGrid = document.getElementById("recommender-grid");
    const recommenderEmpty = document.getElementById("recommender-empty");
    const error = document.getElementById("form-error");
    const submit = document.getElementById("submit-button");
    const profileKey = "brainlab_server_access_profile_v1";
    let touched = false;
    let submitted = false;

    telegram?.ready();
    telegram?.expand();
    const user = telegram?.initDataUnsafe?.user;
    fields["full-name"].value = [user?.first_name, user?.last_name].filter(Boolean).join(" ");
    fields.username.value = user?.username?.toLowerCase() || "";

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
        if (!Array.isArray(parsed)) return [];
        return parsed.filter((item) =>
            item && Number.isInteger(item.id) && item.id > 0 && typeof item.label === "string");
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
            recommenderGrid.querySelectorAll('input[name="recommender"]:checked'));
        return {
            ids: checked.map((input) => Number(input.value)),
            labels: checked.map((input) =>
                candidates.find((item) => item.id === Number(input.value))?.label || ""),
        };
    }

    function applySelectedIds(ids) {
        const wanted = new Set((ids || []).map(Number));
        for (const input of recommenderGrid.querySelectorAll('input[name="recommender"]')) {
            input.checked = wanted.has(Number(input.value));
        }
    }

    function fillDraft(key, apply) {
        cloud?.getItem?.(key, (failure, raw) => {
            if (failure || touched || !raw) return;
            try { apply(JSON.parse(raw)); } catch { /* Ignore a malformed saved draft. */ }
        });
    }
    fillDraft(profileKey, (draft) => {
        if (typeof draft.full_name === "string") fields["full-name"].value = draft.full_name;
        if (Array.isArray(draft.recommender_ids)) applySelectedIds(draft.recommender_ids);
    });
    fillDraft("brainlab_server_request_v1", (draft) => {
        if (typeof draft.server_username === "string") fields.username.value = draft.server_username.toLowerCase();
    });

    fields.username.addEventListener("input", () => {
        const lowered = fields.username.value.toLowerCase();
        if (lowered !== fields.username.value) {
            const caret = fields.username.selectionStart;
            fields.username.value = lowered;
            fields.username.setSelectionRange(caret, caret);
        }
    });
    form.addEventListener("input", (event) => {
        touched = true;
        event.target.setCustomValidity?.("");
        error.hidden = true;
    });
    form.addEventListener("change", () => { touched = true; });
    form.addEventListener("submit", (event) => {
        event.preventDefault();
        if (submitted) return;
        const name = fields["full-name"].value.trim();
        const words = name.split(/\s+/);
        fields["full-name"].setCustomValidity(words.length >= 2
            && words.every((word) => /^(?=.*\p{L})[\p{L}-]+$/u.test(word)) ? "" : "Укажите имя и фамилию.");
        if (!form.reportValidity()) return;
        if (!telegram?.sendData) {
            error.textContent = "Откройте эту форму кнопкой «База знаний MCP» внутри Telegram-бота.";
            error.hidden = false;
            return;
        }
        const selection = selectedRecommenders();
        const data = {
            version: 1,
            kind: "mcp_request",
            full_name: name,
            username: fields.username.value.trim().toLowerCase(),
            recommender: selection.labels.join(", "),
            recommender_ids: selection.ids,
            requested_role: form.querySelector('input[name="role"]:checked').value,
            activity: fields.activity.value.trim(),
            access_term: "бессрочно",
        };
        cloud?.setItem?.(profileKey, JSON.stringify({
            full_name: data.full_name, recommender: data.recommender,
            recommender_ids: selection.ids,
        }));
        submitted = true;
        submit.disabled = true;
        submit.querySelector("span").textContent = "Отправляю…";
        telegram.sendData(JSON.stringify(data));
    });
})();
