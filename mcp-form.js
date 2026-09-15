(() => {
    "use strict";

    const telegram = window.Telegram?.WebApp;
    const cloud = telegram?.isVersionAtLeast?.("6.9") ? telegram.CloudStorage : null;
    const form = document.getElementById("server-form");
    const fields = Object.fromEntries(["full-name", "username", "recommender", "activity"]
        .map((id) => [id, document.getElementById(id)]));
    const error = document.getElementById("form-error");
    const submit = document.getElementById("submit-button");
    let touched = false;
    let submitted = false;

    telegram?.ready();
    telegram?.expand();
    const user = telegram?.initDataUnsafe?.user;
    fields["full-name"].value = [user?.first_name, user?.last_name].filter(Boolean).join(" ");
    fields.username.value = user?.username?.toLowerCase() || "";

    function fillDraft(key, apply) {
        cloud?.getItem?.(key, (failure, raw) => {
            if (failure || touched || !raw) return;
            try { apply(JSON.parse(raw)); } catch { /* Ignore a malformed saved draft. */ }
        });
    }
    fillDraft("brainlab_server_access_profile_v1", (draft) => {
        if (typeof draft.full_name === "string") fields["full-name"].value = draft.full_name;
        if (typeof draft.recommender === "string") fields.recommender.value = draft.recommender;
    });
    fillDraft("brainlab_server_request_v1", (draft) => {
        if (typeof draft.server_username === "string") fields.username.value = draft.server_username;
    });
    form.addEventListener("input", (event) => {
        touched = true;
        event.target.setCustomValidity?.("");
        error.hidden = true;
    });
    form.addEventListener("submit", (event) => {
        event.preventDefault();
        if (submitted) return;
        const name = fields["full-name"].value.trim();
        const words = name.split(/\s+/);
        fields["full-name"].setCustomValidity(words.length >= 2
            && words.every((word) => /^(?=.*\p{L})[\p{L}-]+$/u.test(word)) ? "" : "Укажите имя и фамилию.");
        fields.recommender.setCustomValidity(fields.recommender.value.trim() ? "" : "Укажите рекомендателя.");
        if (!form.reportValidity()) return;
        if (!telegram?.sendData) {
            error.textContent = "Откройте эту форму кнопкой «База знаний MCP» внутри Telegram-бота.";
            error.hidden = false;
            return;
        }
        const data = {
            version: 1,
            kind: "mcp_request",
            full_name: name,
            username: fields.username.value.trim(),
            recommender: fields.recommender.value.trim(),
            requested_role: form.querySelector('input[name="role"]:checked').value,
            activity: fields.activity.value.trim(),
            access_term: "бессрочно",
        };
        cloud?.setItem?.("brainlab_server_access_profile_v1", JSON.stringify({
            full_name: data.full_name, recommender: data.recommender,
        }));
        submitted = true;
        submit.disabled = true;
        submit.querySelector("span").textContent = "Отправляю…";
        telegram.sendData(JSON.stringify(data));
    });
})();
