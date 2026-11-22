(() => {
  const CATEGORY_IDS = ["all", "attention", "updates", "consulting", "newsletters", "missed"];

  const state = {
    locale: "en",
    messages: {},
    availableLocales: [],
    mode: "inbox",
    category: "all",
    account: "all",
    unreadOnly: false,
    search: "",
    data: null,
    selectedUids: new Set(),
    actionsByUid: new Map(),
    confirmCallback: null,
    lastJobResults: [],
    loadSeq: 0,
    rulesFilter: "all",
    rulesSearch: "",
    rulesSelectedKeys: new Set(),
    rulesItems: [],
    rulesSeq: 0,
    auditStatus: "all",
    auditRunId: "",
    auditSearch: "",
    auditItems: [],
    auditRuns: [],
    auditSeq: 0,
    updateInfo: null,
  };

  const qs = (id) => document.getElementById(id);

  function escapeHtml(str) {
    return String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function t(key, vars = {}) {
    const raw = Object.prototype.hasOwnProperty.call(state.messages, key)
      ? state.messages[key]
      : key;
    return String(raw).replace(/\{([a-zA-Z0-9_]+)\}/g, (match, name) =>
      Object.prototype.hasOwnProperty.call(vars, name) && vars[name] !== undefined
        ? String(vars[name])
        : match
    );
  }

  function tHtml(key, vars = {}, rawHtmlKeys = []) {
    const safeVars = {};
    const rawSet = new Set(rawHtmlKeys);
    for (const [k, v] of Object.entries(vars)) {
      safeVars[k] = rawSet.has(k) ? String(v ?? "") : escapeHtml(v);
    }
    return t(key, safeVars);
  }

  function actionLabel(act) {
    const key = `action.${act}`;
    return Object.prototype.hasOwnProperty.call(state.messages, key)
      ? t(key)
      : String(act || "");
  }

  function categoryLabel(id) {
    const key = `cat.${id}`;
    return Object.prototype.hasOwnProperty.call(state.messages, key)
      ? t(key)
      : String(id || "");
  }

  function translateStaticDom() {
    document.documentElement.lang = state.locale || "en";
    document.title = t("app.title");
    for (const el of document.querySelectorAll("[data-i18n]")) {
      const k = el.getAttribute("data-i18n");
      if (k) el.textContent = t(k);
    }
    for (const el of document.querySelectorAll("[data-i18n-title]")) {
      const k = el.getAttribute("data-i18n-title");
      if (k) el.setAttribute("title", t(k));
    }
    for (const el of document.querySelectorAll("[data-i18n-placeholder]")) {
      const k = el.getAttribute("data-i18n-placeholder");
      if (k) el.setAttribute("placeholder", t(k));
    }
    if (!state.data) {
      if (qs("btn-exec-selected")) qs("btn-exec-selected").textContent = t("btn.exec_selected", { count: 0 });
      if (qs("btn-exec-all")) qs("btn-exec-all").textContent = t("btn.exec_all", { count: 0 });
    }
  }

  async function loadI18n(langOverride = "") {
    const q = langOverride ? `?lang=${encodeURIComponent(langOverride)}` : "";
    const payload = await api(`/api/i18n${q}`);
    state.locale = payload.language || "en";
    state.messages = payload.messages || {};
    state.availableLocales = payload.available || [];
    translateStaticDom();
    renderUpdateBanner();
    if (state.data) render();
  }

  function renderUpdateBanner() {
    const el = qs("update-banner");
    if (!el) return;
    const info = state.updateInfo;
    if (!info || !info.update_available || !info.latest_version) {
      el.classList.add("hidden");
      el.innerHTML = "";
      return;
    }
    try {
      if (window.localStorage && window.localStorage.getItem("nollpost_dismissed_update") === String(info.latest_version)) {
        el.classList.add("hidden");
        el.innerHTML = "";
        return;
      }
    } catch (_ignored) {
      // Ignore storage access restriction
    }
    const relLink = isSafeHttpUrl(info.release_url)
      ? `<a class="btn small secondary" href="${escapeHtml(info.release_url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(t("update.release_notes"))}</a>`
      : "";
    el.innerHTML = `
      <div>${tHtml("update.banner", {
        latest: info.latest_version,
        current: info.current_version || "",
        cmd: info.upgrade_command || "brew upgrade hessius/tap/nollpost",
      })}</div>
      <div class="update-banner-actions">
        <button type="button" class="btn small primary" id="btn-copy-upgrade-cmd" data-upgrade-cmd="${escapeHtml(info.upgrade_command || "brew upgrade hessius/tap/nollpost")}">${escapeHtml(t("update.copy_cmd"))}</button>
        ${relLink}
        <button type="button" class="btn small secondary" id="btn-dismiss-update" data-dismiss-ver="${escapeHtml(info.latest_version)}">${escapeHtml(t("update.dismiss"))}</button>
      </div>
    `;
    el.classList.remove("hidden");
  }

  async function checkForUpdates(refresh = false) {
    try {
      const info = await api(`/api/updates?refresh=${refresh ? "true" : "false"}`);
      state.updateInfo = info;
      renderUpdateBanner();
    } catch (_ignored) {
      // Offline or blocked update check never disturbs triage workflow
    }
  }

  function accountToneClass(name) {
    const s = String(name || "").toLowerCase();
    let hash = 5381;
    for (let i = 0; i < s.length; i++) {
      hash = ((hash << 5) + hash + s.charCodeAt(i)) >>> 0;
    }
    return `acct-c${hash % 6}`;
  }

  function isSafeHttpUrl(url) {
    return /^https?:\/\//i.test(String(url || "").trim());
  }

  function isMailPermissionError(msg) {
    return /-1743|not authori[sz]ed|osascript|applescript|privilege|automation|apple events|accessibil/i.test(String(msg || ""));
  }

  function renderRecoveryCard(rawMessage) {
    const perm = isMailPermissionError(rawMessage);
    const title = perm ? t("err.permission_title") : t("err.scan_failed_title");
    const stepsHtml = perm
      ? `<p>${t("err.permission_intro")}</p>
         <ol style="margin-left:18px;line-height:1.5;">
           <li>${t("err.permission_step1")}</li>
           <li>${t("err.permission_step2")}</li>
           <li>${t("err.permission_step3")}</li>
         </ol>`
      : `<p>${t("err.scan_failed_intro")}</p>`;
    return `
      <div class="empty-state scan-error">
        <h3>${escapeHtml(title)}</h3>
        ${stepsHtml}
        <pre style="white-space:pre-wrap;word-break:break-word;max-width:100%;font-size:11px;background:var(--bg-primary);padding:8px 10px;border-radius:8px;border:1px solid var(--border);">${escapeHtml(rawMessage)}</pre>
        <div class="empty-state-actions">
          <button class="btn small primary" data-retry-scan="1">${escapeHtml(t("err.retry_scan"))}</button>
        </div>
      </div>`;
  }

  function showError(msg) {
    const resEl = qs("results-banner");
    resEl.dataset.kind = "error";
    resEl.classList.remove("hidden");
    const tip = isMailPermissionError(msg)
      ? `<div style="margin-top:4px;font-size:11px;color:var(--text-secondary);">${t("err.banner_tip")}</div>`
      : "";
    resEl.innerHTML = `<div class="result-row"><div><span class="pill danger">${escapeHtml(t("err.badge"))}</span> ${escapeHtml(msg)}${tip}</div></div>`;
  }

  async function api(path, options = {}) {
    const resp = await fetch(path, {
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    if (!resp.ok) {
      const txt = await resp.text();
      throw new Error(txt || `HTTP ${resp.status}`);
    }
    return resp.json();
  }

  function hydrateActionsFromData(data) {
    const nextMap = new Map();
    for (const item of data.items || []) {
      const prev = state.actionsByUid.get(item.uid);
      if (prev && (prev === "no_action" || item.allowed_actions.includes(prev))) {
        nextMap.set(item.uid, prev);
      } else {
        nextMap.set(item.uid, item.default_action);
      }
    }
    state.actionsByUid = nextMap;
    const validUids = new Set((data.items || []).map((i) => i.uid));
    state.selectedUids = new Set([...state.selectedUids].filter((u) => validUids.has(u)));
  }

  function filteredItems() {
    if (!state.data || state.category === "missed") return [];
    const term = state.search.trim().toLowerCase();
    return (state.data.items || []).filter((item) => {
      if (state.category !== "all" && item.category !== state.category) return false;
      if (state.account !== "all" && item.header.account !== state.account) return false;
      if (state.unreadOnly && item.header.is_read) return false;
      if (term) {
        const hay = `${item.header.subject} ${item.header.sender} ${item.subcategory}`.toLowerCase();
        if (!hay.includes(term)) return false;
      }
      return true;
    });
  }

  function nonNoopPairs(itemsList) {
    const pairs = [];
    for (const item of itemsList) {
      const act = state.actionsByUid.get(item.uid) || item.default_action;
      if (act && act !== "no_action") {
        pairs.push({ uid: item.uid, action: act });
      }
    }
    return pairs;
  }

  function renderHeader() {
    const d = state.data;
    if (!d) return;
    const acctParts = Object.entries(d.briefing.per_account || {}).map(
      ([name, c]) => `${name} (${c.total})`
    );
    const acctSuffix = acctParts.length ? ` · ${acctParts.join(" · ")}` : "";
    const baseTitle = state.mode === "catchup" ? t("board.title_catchup") : t("board.title_inbox");
    qs("board-title").textContent = `${baseTitle}${acctSuffix}`;

    const st = d.stats || {};
    qs("tally-pills").innerHTML = `
      <span class="pill green">${escapeHtml(t("tally.lifetime_unsub", { total: st.total_unsubscribed || 0, senders: st.unique_senders_unsubscribed || 0 }))}</span>
      <span class="pill accent">${escapeHtml(t("tally.archived", { count: st.total_archived || 0 }))}</span>
      <span class="pill accent">${escapeHtml(t("tally.declined", { count: st.total_declined || 0 }))}</span>
      <span class="pill muted">${escapeHtml(t("tally.zero_deleted"))}</span>
      <span class="pill warn">${escapeHtml(t("tally.missed_queue", { count: st.missed_unsub_pending || 0 }))}</span>
    `;

    qs("mode-inbox").classList.toggle("active", state.mode === "inbox");
    qs("mode-catchup").classList.toggle("active", state.mode === "catchup");

    const allItems = d.items || [];
    const visItems = filteredItems();
    const selItems = allItems.filter((i) => state.selectedUids.has(i.uid));

    const allPairs = nonNoopPairs(allItems);
    const catPairs = nonNoopPairs(visItems);
    const selPairs = nonNoopPairs(selItems);

    const catLabel = CATEGORY_IDS.includes(state.category)
      ? categoryLabel(state.category)
      : t("cat.current_fallback");
    qs("btn-reset-actions").disabled = catPairs.length === 0 || state.category === "missed";
    qs("btn-exec-selected").textContent = t("btn.exec_selected", { count: selPairs.length });
    qs("btn-exec-selected").disabled = selPairs.length === 0;
    qs("btn-exec-category").textContent = t("btn.exec_category", { category: catLabel, count: catPairs.length });
    qs("btn-exec-category").disabled = catPairs.length === 0 || state.category === "missed";
    qs("btn-exec-all").textContent = t("btn.exec_all", { count: allPairs.length });
    qs("btn-exec-all").disabled = allPairs.length === 0;

    const counts = {};
    for (const p of allPairs) {
      counts[p.action] = (counts[p.action] || 0) + 1;
    }
    const summaryBits = Object.entries(counts).map(([k, v]) => `${v} ${actionLabel(k)}`);
    const warnTxt = (d.warnings || []).length
      ? t("batch.warnings", { warnings: d.warnings.join("; ") })
      : "";
    qs("batch-summary").textContent =
      (summaryBits.length
        ? t("batch.queued_defaults", { summary: summaryBits.join(" · ") })
        : t("batch.none_pending")) + warnTxt;

    const tray = d.tray || [];
    const trayEl = qs("safety-tray");
    if (tray.length > 0) {
      trayEl.classList.remove("hidden");
      const names = tray.slice(0, 4).map((item) => escapeHtml(item.subject.slice(0, 36))).join(", ");
      const more = tray.length > 4 ? escapeHtml(t("tray.more", { count: tray.length - 4 })) : "";
      qs("tray-text").innerHTML = tHtml("tray.summary", { count: tray.length, names, more }, ["names", "more"]);
      qs("btn-tray-sweep").textContent = t("tray.sweep_btn", { count: tray.length });
    } else {
      trayEl.classList.add("hidden");
    }
  }

  function renderTabs() {
    const d = state.data;
    if (!d) return;
    const allItems = d.items || [];
    const catCounts = {
      all: allItems.length,
      attention: allItems.filter((i) => i.category === "attention").length,
      updates: allItems.filter((i) => i.category === "updates").length,
      consulting: allItems.filter((i) => i.category === "consulting").length,
      newsletters: allItems.filter((i) => i.category === "newsletters").length,
      missed: (d.missed || []).length,
    };
    const visibleCats = CATEGORY_IDS.filter(
      (id) => id !== "consulting" || d.consulting_enabled || catCounts.consulting > 0
    );
    if (!visibleCats.includes(state.category)) {
      state.category = "all";
    }
    qs("category-tabs").innerHTML = visibleCats.map(
      (id) =>
        `<button class="tab ${state.category === id ? "active" : ""}" data-cat="${id}">${escapeHtml(categoryLabel(id))} (${catCounts[id] || 0})</button>`
    ).join("");

    const acctList = d.accounts || [];
    if (state.account !== "all" && !acctList.some((a) => a.name === state.account)) {
      state.account = "all";
    }
    if (acctList.length <= 1) {
      qs("account-tabs").innerHTML = "";
    } else {
      const accts = [{ id: "all", label: t("filter.all_accounts", { count: allItems.length }) }];
      for (const a of acctList) {
        const cnt = allItems.filter((i) => i.header.account === a.name).length;
        accts.push({ id: a.name, label: `${a.name} (${cnt})` });
      }
      qs("account-tabs").innerHTML = accts
        .map(
          (a) =>
            `<button class="tab ${state.account === a.id ? "active" : ""}" data-acct="${escapeHtml(a.id)}">${escapeHtml(a.label)}</button>`
        )
        .join("");
    }
  }

  function pickBestMissedLink(m) {
    if (isSafeHttpUrl(m.final_url)) return m.final_url;
    const links = Array.isArray(m.candidate_links) ? m.candidate_links : [];
    const unsubRe = /unsub|opt[-_]?out|avregistrera|avprenumerera|preference|subscription_center/i;
    for (let i = links.length - 1; i >= 0; i--) {
      const h = links[i]?.href || "";
      const txt = `${links[i]?.text || ""} ${h}`;
      if (isSafeHttpUrl(h) && unsubRe.test(txt)) return h;
    }
    for (const c of links) {
      if (isSafeHttpUrl(c?.href)) return c.href;
    }
    return "";
  }

  function renderMissedQueue() {
    const missed = state.data?.missed || [];
    if (!missed.length) {
      qs("queue-container").innerHTML = `<div style="padding:20px;text-align:center;color:var(--text-secondary);">${escapeHtml(t("empty.missed"))}</div>`;
      return;
    }
    const rows = missed
      .map((m) => {
        const firstLink = pickBestMissedLink(m);
        return `
        <tr>
          <td><span class="acct ${accountToneClass(m.account)}">${escapeHtml(m.account)}</span></td>
          <td><span class="pill ${m.stage === "playwright" ? "danger" : "warn"}">${escapeHtml(m.stage)}</span></td>
          <td>
            <strong>${escapeHtml(m.sender)}</strong> · ${escapeHtml(m.subject)}
            <div class="sub-meta">
              <span>${escapeHtml(t("missed.msg_id", { id: m.internal_id }))}</span>
              <span>· ${escapeHtml(m.failure_reason)}</span>
            </div>
          </td>
          <td>
            <div class="seg">
              <button data-missed-inspect="${m.id}">${escapeHtml(t("missed.inspect"))}</button>
              ${firstLink ? `<button data-missed-open="${escapeHtml(firstLink)}">${escapeHtml(t("missed.open_link"))}</button>` : ""}
              ${firstLink ? `<button data-missed-retry="${m.id}">${escapeHtml(t("missed.retry"))}</button>` : ""}
              <button data-missed-mark-unsub="${m.id}">${escapeHtml(t("missed.mark_unsub_archive"))}</button>
              <button data-missed-resolve="${m.id}">${escapeHtml(t("missed.dismiss"))}</button>
            </div>
          </td>
        </tr>`;
      })
      .join("");

    qs("queue-container").innerHTML = `
      <table class="tbl">
        <thead>
          <tr><th>${escapeHtml(t("tbl.col_acct"))}</th><th>${escapeHtml(t("tbl.col_stage"))}</th><th>${escapeHtml(t("tbl.col_missed_reason"))}</th><th>${escapeHtml(t("tbl.col_actions"))}</th></tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>`;
  }

  function renderTable() {
    if (state.category === "missed") {
      renderMissedQueue();
      return;
    }
    const rowsData = filteredItems();
    if (!rowsData.length) {
      const allItems = state.data?.items || [];
      const warnings = state.data?.warnings || [];
      if (warnings.length && !allItems.length) {
        qs("queue-container").innerHTML = renderRecoveryCard(warnings.join("\n"));
        return;
      }
      if (!allItems.length) {
        const isCatchup = state.mode === "catchup";
        qs("queue-container").innerHTML = `
          <div class="empty-state inbox-zero">
            <h3>${escapeHtml(isCatchup ? t("empty.catchup_title") : t("empty.inbox_zero_title"))}</h3>
            <p>${escapeHtml(isCatchup ? t("empty.catchup_body") : t("empty.inbox_zero_body"))}</p>
            <div class="empty-state-actions">
              <button class="btn small primary" data-retry-scan="1">${escapeHtml(t("ctrl.refresh"))}</button>
              ${!isCatchup
                ? `<button class="btn small secondary" data-mode="catchup">${escapeHtml(t("empty.check_catchup"))}</button>`
                : `<button class="btn small secondary" data-mode="inbox">${escapeHtml(t("empty.back_inbox"))}</button>`}
            </div>
          </div>`;
        return;
      }
      qs("queue-container").innerHTML = `
        <div class="empty-state">
          <h3>${escapeHtml(t("empty.filtered_title"))}</h3>
          <p>${escapeHtml(t("empty.filtered_body"))}</p>
          <div class="empty-state-actions">
            <button class="btn small primary" id="btn-reset-filters">${escapeHtml(t("empty.reset_filters"))}</button>
          </div>
        </div>`;
      return;
    }
    const allVisibleSelected = rowsData.every((i) => state.selectedUids.has(i.uid));

    const rowsHtml = rowsData
      .map((item) => {
        const h = item.header;
        const checked = state.selectedUids.has(item.uid);
        const curAction = state.actionsByUid.get(item.uid) || item.default_action;
        const acctCls = accountToneClass(h.account);
        const catPillCls =
          item.category === "attention"
            ? "warn"
            : item.category === "updates"
            ? "green"
            : item.category === "consulting"
            ? "accent"
            : "muted";

        const badgesHtml = (item.badges || [])
          .map((b) => `<span class="pill ${b.includes("Sent after") || b.includes("No unsub") ? "warn" : "muted"}">${escapeHtml(b)}</span>`)
          .join("");
        const countBadge = item.group_count > 1 ? `<span class="pill accent">${item.group_count}×</span>` : "";

        const buttonsOrder =
          state.mode === "catchup"
            ? ["no_action", "unsub_only"]
            : [
                "no_action",
                "mark_read",
                ...(item.allowed_actions.includes("read_show_invoice") ? ["read_show_invoice"] : []),
                ...(item.allowed_actions.includes("read_open_link") ? ["read_open_link"] : []),
                ...(item.allowed_actions.includes("decline_and_archive") ? ["decline_and_archive"] : []),
                ...(item.allowed_actions.includes("draft_decline") ? ["draft_decline", "send_decline_and_archive"] : []),
                "archive",
                "archive_and_unsub",
              ];

        const segHtml = buttonsOrder
          .map((act) => {
            const allowed = item.allowed_actions.includes(act);
            const active = curAction === act;
            let title = "";
            if (!allowed && act === "archive_and_unsub") {
              title = t("row.disabled_unsub_title");
            } else if (act === "read_open_link" && item.extracted?.pertinent_url) {
              title = item.extracted.pertinent_url;
            }
            return `<button class="${active ? "active" : ""}" ${!allowed ? "disabled" : ""} title="${escapeHtml(title)}" data-uid="${escapeHtml(item.uid)}" data-act="${act}">${escapeHtml(actionLabel(act))}</button>`;
          })
          .join("");

        const queueMissedBtn =
          item.no_unsub_found && state.mode === "inbox"
            ? `<button class="btn small" data-enqueue-missed="${escapeHtml(item.uid)}">${escapeHtml(t("row.queue_missed"))}</button>`
            : "";

        return `
        <tr class="${checked ? "selected-row" : ""} ${h.is_read ? "read-row" : "unread-row"}">
          <td><input type="checkbox" class="row-cb" data-uid="${escapeHtml(item.uid)}" ${checked ? "checked" : ""} /></td>
          <td><span class="acct ${acctCls}">${escapeHtml(h.account)}</span></td>
          <td>
            <span class="pill ${catPillCls}">${escapeHtml(item.subcategory)}</span>
            ${countBadge}
          </td>
          <td>
            <strong>${escapeHtml(h.subject || t("row.no_subject"))}</strong>
            <div class="sub-meta">
              <span>${escapeHtml(h.sender)}</span>
              <span>· ${escapeHtml((h.date_received || "").slice(0, 16).replace("T", " "))}</span>
              ${badgesHtml}
              <select class="pref-select" data-pref-email="${escapeHtml(h.sender_email)}" data-pref-dom="${escapeHtml(h.sender_domain)}" data-pref-name="${escapeHtml(h.sender)}">
                <option value="">${escapeHtml(t("pref.remember"))}</option>
                <option value="always_keep">${escapeHtml(t("pref.always_keep"))}</option>
                <option value="archive">${escapeHtml(t("pref.always_archive"))}</option>
                <option value="mark_read">${escapeHtml(t("pref.always_mark_read"))}</option>
                <option value="clear">${escapeHtml(t("pref.clear"))}</option>
              </select>
              ${queueMissedBtn}
            </div>
          </td>
          <td><div class="seg">${segHtml}</div></td>
        </tr>`;
      })
      .join("");

    qs("queue-container").innerHTML = `
      <table class="tbl">
        <thead>
          <tr>
            <th style="width:32px;"><input type="checkbox" id="master-cb" ${allVisibleSelected ? "checked" : ""} /></th>
            <th>${escapeHtml(t("tbl.col_acct"))}</th>
            <th>${escapeHtml(t("tbl.col_category"))}</th>
            <th>${escapeHtml(t("tbl.col_subject"))}</th>
            <th>${escapeHtml(t("tbl.col_action"))}</th>
          </tr>
        </thead>
        <tbody>${rowsHtml}</tbody>
      </table>`;
  }

  function render() {
    renderTabs();
    renderHeader();
    renderTable();
  }

  async function loadState(refresh = false) {
    const seq = ++state.loadSeq;
    const modeLabel = state.mode === "catchup" ? t("mode.catchup") : t("mode.inbox");
    qs("queue-container").innerHTML = `<div style="padding:20px;color:var(--text-secondary);" id="scan-status-text">${escapeHtml(t("scan.loading", { mode: modeLabel }))}</div>`;
    let pollTimer = null;
    if (refresh || !state.data || state.data.mode !== state.mode) {
      pollTimer = setInterval(async () => {
        try {
          const st = await api("/api/scan/status");
          if (seq !== state.loadSeq) return;
          const el = qs("scan-status-text");
          if (el && st.active && st.message) {
            el.textContent = st.message;
          }
        } catch (_ignored) {
          // Ignore transient polling errors while scan completes
        }
      }, 350);
    }
    try {
      const data = await api(`/api/state?mode=${encodeURIComponent(state.mode)}&refresh=${refresh ? "true" : "false"}`);
      if (seq !== state.loadSeq) return;
      state.data = data;
      const resEl = qs("results-banner");
      if (resEl.dataset.kind === "error") {
        resEl.classList.add("hidden");
        resEl.innerHTML = "";
        delete resEl.dataset.kind;
      }
      hydrateActionsFromData(data);
      render();
    } catch (err) {
      if (seq === state.loadSeq) {
        qs("queue-container").innerHTML = renderRecoveryCard(err.message);
      }
      throw err;
    } finally {
      if (pollTimer) clearInterval(pollTimer);
    }
  }

  async function openConfirmation(title, pairs) {
    const preview = await api("/api/execute/preview", {
      method: "POST",
      body: JSON.stringify({
        mode: state.mode,
        items: pairs,
        headless: !qs("show-browser-toggle").checked,
      }),
    });

    const archSummary = Object.entries(preview.archive_targets || {})
      .map(([acct, boxes]) =>
        Object.entries(boxes)
          .map(([b, c]) => `${acct} → ${b} (${c})`)
          .join(", ")
      )
      .join(" | ");

    const statsGrid = Object.entries(preview.counts || {})
      .map(([act, cnt]) => `<div class="confirm-stat"><strong>${cnt}</strong>${escapeHtml(actionLabel(act))}</div>`)
      .join("");

    let detailsHtml = "";
    if ((preview.send_declines || []).length > 0) {
      detailsHtml += `<div class="confirm-warning danger">${tHtml("confirm.warn_outbound", { count: preview.send_declines.length })}</div>`;
    }
    if ((preview.unsubs || []).length > 10) {
      detailsHtml += `<div class="confirm-warning warn">${tHtml("confirm.warn_large_unsub", { count: preview.unsubs.length })}</div>`;
    }
    if (archSummary) {
      detailsHtml += `<div class="preview-box"><strong>${escapeHtml(t("confirm.archive_dest"))}</strong> ${escapeHtml(archSummary)}</div>`;
    }
    if ((preview.links_to_open || []).length) {
      detailsHtml += `<div class="preview-box"><strong>${escapeHtml(t("confirm.open_links"))}</strong><br/>${preview.links_to_open
        .map((l) => `• ${escapeHtml(l.subject)} → <code>${escapeHtml(l.url)}</code>`)
        .join("<br/>")}</div>`;
    }
    if ((preview.pdfs_to_open || []).length) {
      detailsHtml += `<div class="preview-box"><strong>${escapeHtml(t("confirm.open_pdfs"))}</strong><br/>${preview.pdfs_to_open
        .map((p) => `• ${escapeHtml(p.subject)} (${escapeHtml(p.filename)})`)
        .join("<br/>")}</div>`;
    }
    if ((preview.unsubs || []).length) {
      detailsHtml += `<div class="preview-box"><strong>${escapeHtml(t("confirm.unsub_senders", { count: preview.unsubs.length }))}</strong><br/>${preview.unsubs
        .map((u) => `• ${escapeHtml(u.sender)} [${escapeHtml(u.method)}] → <code>${escapeHtml(u.url)}</code>`)
        .join("<br/>")}</div>`;
    }
    if ((preview.declines || []).length) {
      detailsHtml += `<div class="preview-box"><strong>${escapeHtml(t("confirm.consulting_declines"))}</strong><br/>${preview.declines
        .map((d) => `• ${escapeHtml(d.sender)} · ${escapeHtml(d.subject)} → <code>${escapeHtml(d.url)}</code>`)
        .join("<br/>")}</div>`;
    }
    if ((preview.draft_declines || []).length) {
      detailsHtml += `<div class="preview-box"><strong>${escapeHtml(t("confirm.draft_declines"))}</strong><br/>${preview.draft_declines
        .map((d) => `• ${escapeHtml(d.sender)} · ${escapeHtml(d.subject)}`)
        .join("<br/>")}</div>`;
    }
    if ((preview.send_declines || []).length) {
      detailsHtml += `<div class="preview-box"><strong>${escapeHtml(t("confirm.send_declines"))}</strong><br/>${preview.send_declines
        .map((d) => `• ${escapeHtml(d.sender)} · ${escapeHtml(d.subject)}`)
        .join("<br/>")}<pre style="margin-top:6px;">${escapeHtml(preview.boilerplate_preview)}</pre></div>`;
    }

    qs("modal-title").textContent = t("confirm.title_with_count", { title, count: preview.total_actions });
    qs("modal-body").innerHTML = `<div class="confirm-grid">${statsGrid}</div>${detailsHtml}`;
    qs("modal-confirm").disabled = false;
    qs("confirm-modal").classList.remove("hidden");

    state.confirmCallback = async () => {
      qs("confirm-modal").classList.add("hidden");
      await runConfirmedBatch(pairs);
    };
  }

  async function runConfirmedBatch(pairs) {
    const progEl = qs("progress-banner");
    const resEl = qs("results-banner");
    resEl.classList.add("hidden");
    progEl.classList.remove("hidden");
    progEl.textContent = t("run.starting", { total: pairs.length });

    try {
      const { job_id } = await api("/api/execute/run", {
        method: "POST",
        body: JSON.stringify({
          mode: state.mode,
          items: pairs,
          headless: !qs("show-browser-toggle").checked,
        }),
      });

      while (true) {
        await new Promise((r) => setTimeout(r, 600));
        const job = await api(`/api/jobs/${encodeURIComponent(job_id)}`);
        progEl.textContent = t("run.progress", { done: job.done, total: job.total, message: job.message });
        if (job.status === "completed") {
          progEl.classList.add("hidden");
          renderJobResults(job.results || [], job.scan_warning || "");
          await loadState(false);
          break;
        }
        if (job.status === "error") {
          progEl.textContent = t("run.error", { message: job.message });
          break;
        }
      }
    } catch (err) {
      progEl.classList.add("hidden");
      showError(err.message);
    }
  }

  function renderJobResults(results, scanWarning = "") {
    const resEl = qs("results-banner");
    state.lastJobResults = results || [];
    if (!results.length && !scanWarning) return;
    resEl.dataset.kind = "results";
    resEl.classList.remove("hidden");
    const warnRow = scanWarning
      ? `<div class="result-row"><div><span class="pill warn">${escapeHtml(t("results.warning"))}</span> ${escapeHtml(scanWarning)}</div></div>`
      : "";
    const rows = results
      .map((r, idx) => {
        const cls = r.status === "ok" || r.status === "drafted" ? "green" : r.status === "blocked_captcha" ? "warn" : "danger";
        const btns = [];
        if (r.status === "blocked_captcha" && isSafeHttpUrl(r.opened_target)) {
          btns.push(`<button class="btn small" data-open-url="${escapeHtml(r.opened_target)}">${escapeHtml(t("results.open_browser"))}</button>`);
        }
        if (r.screenshot_path) {
          btns.push(`<button class="btn small" data-proof-idx="${idx}">${escapeHtml(t("results.view_screenshot"))}</button>`);
        } else if (isSafeHttpUrl(r.opened_target) && ["archive_and_unsub", "unsub_only", "decline_and_archive"].includes(r.action)) {
          btns.push(`<button class="btn small" data-proof-idx="${idx}">${escapeHtml(t("results.view_proof"))}</button>`);
        }
        if (r.status !== "ok" && ["archive_and_unsub", "unsub_only"].includes(r.action)) {
          const lbl = r.action === "unsub_only" ? t("results.mark_unsub") : t("results.mark_unsub_archive");
          btns.push(`<button class="btn small" data-mark-unsub-idx="${idx}">${escapeHtml(lbl)}</button>`);
        }
        return `<div class="result-row">
          <div><span class="pill ${cls}">${escapeHtml(r.status)}</span> <strong>${escapeHtml(r.sender)}</strong> · ${escapeHtml(r.detail)}</div>
          <div style="display:flex;gap:6px;flex-wrap:wrap;">${btns.join("")}</div>
        </div>`;
      })
      .join("");
    resEl.innerHTML = `<div style="display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:6px;margin-bottom:6px;"><strong>${escapeHtml(t("results.header", { count: results.length }))}</strong><span style="font-size:11px;color:var(--text-secondary);">${escapeHtml(t("results.reset_note"))}</span></div>${warnRow}${rows}`;
  }

  function showProofModal(idx) {
    const r = state.lastJobResults[idx];
    if (!r) return;
    qs("snap-title").textContent = t("proof.title", { sender: r.sender });
    const shotName = r.screenshot_path ? r.screenshot_path.split("/").pop() : "";
    const shotHtml = shotName
      ? `<div class="preview-box"><strong>${escapeHtml(t("proof.screenshot"))}</strong><br/><img src="/api/screenshots/${encodeURIComponent(shotName)}" style="max-width:100%;border-radius:6px;margin-top:6px;" /></div>`
      : `<div class="preview-box" style="color:var(--text-secondary);">${escapeHtml(t("proof.tier1_note"))}</div>`;
    const urlHtml = isSafeHttpUrl(r.opened_target)
      ? `<div class="preview-box"><strong>${escapeHtml(t("proof.endpoint"))}</strong><br/><code>${escapeHtml(r.opened_target)}</code></div>`
      : "";
    const markBtnHtml =
      r.status !== "ok" && ["archive_and_unsub", "unsub_only"].includes(r.action)
        ? `<div style="margin-top:8px;"><button class="btn small primary" data-mark-unsub-idx="${idx}">${escapeHtml(
            r.action === "unsub_only" ? t("proof.mark_unsub") : t("proof.mark_unsub_archive")
          )}</button></div>`
        : "";
    qs("snap-body").innerHTML = `
      <div class="preview-box"><strong>${escapeHtml(t("proof.status"))}</strong> ${escapeHtml(r.status)}<br/><strong>${escapeHtml(t("proof.action"))}</strong> ${escapeHtml(actionLabel(r.action))}<br/><strong>${escapeHtml(t("proof.detail"))}</strong> ${escapeHtml(r.detail)}${markBtnHtml}</div>
      ${urlHtml}
      ${shotHtml}
    `;
    qs("snapshot-modal").classList.remove("hidden");
  }

  async function openTrayConfirmation() {
    const tray = state.data?.tray || [];
    if (!tray.length) return;
    const uids = tray.map((tItem) => tItem.message_uid);
    const prev = await api("/api/tray/sweep/preview", {
      method: "POST",
      body: JSON.stringify({ uids }),
    });
    const destStr = Object.entries(prev.archive_targets || {})
      .map(([a, b]) => Object.entries(b).map(([m, c]) => `${a} → ${m} (${c})`).join(", "))
      .join(" | ");
    qs("modal-title").textContent = t("confirm.tray_sweep_title", { count: prev.count });
    const itemRows = (prev.items || [])
      .map((i) => {
        const bestUrl = isSafeHttpUrl(i.latest_pertinent_url)
          ? i.latest_pertinent_url
          : isSafeHttpUrl(i.opened_target)
          ? i.opened_target
          : "";
        const changedTag =
          i.latest_pertinent_url && i.opened_target && i.latest_pertinent_url !== i.opened_target
            ? ` <span class="pill accent">${escapeHtml(t("confirm.updated_link"))}</span>`
            : "";
        const reopenBtn = bestUrl
          ? ` <button type="button" class="btn small" data-open-url="${escapeHtml(bestUrl)}">${escapeHtml(t("confirm.reopen_link"))}</button>`
          : "";
        const dismissItemBtn = `<button type="button" class="btn small secondary" data-tray-dismiss-uid="${escapeHtml(i.uid)}">${escapeHtml(t("confirm.dismiss_item"))}</button>`;
        const urlLine = bestUrl ? `<div style="margin-top:2px;font-size:11px;color:var(--text-secondary);word-break:break-all;"><code>${escapeHtml(bestUrl)}</code></div>` : "";
        return `<div style="padding:6px 0;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;gap:8px;">
          <div>• [${escapeHtml(i.account)}] <strong>${escapeHtml(i.subject)}</strong> · ${escapeHtml(i.sender)}${changedTag}${urlLine}</div>
          <div style="flex-shrink:0;display:flex;gap:6px;">${reopenBtn}${dismissItemBtn}</div>
        </div>`;
      })
      .join("");
    qs("modal-body").innerHTML = `
      <div class="preview-box"><strong>${escapeHtml(t("confirm.tray_sweep_archive"))}</strong> ${escapeHtml(destStr)}</div>
      <div class="preview-box">${itemRows}</div>
    `;
    qs("modal-confirm").disabled = false;
    qs("confirm-modal").classList.remove("hidden");
    state.confirmCallback = async () => {
      qs("confirm-modal").classList.add("hidden");
      await api("/api/tray/sweep", { method: "POST", body: JSON.stringify({ uids }) });
      await loadState(false);
    };
  }

  function showMissedSnapshot(id) {
    const entry = (state.data?.missed || []).find((m) => m.id === id);
    if (!entry) return;
    qs("snap-title").textContent = t("missed_snap.title", { id: entry.id, sender: entry.sender });
    const links = (entry.candidate_links || [])
      .filter((l) => isSafeHttpUrl(l.href))
      .map((l) => `• ${escapeHtml(l.text || t("missed_snap.no_text"))} → <a href="${escapeHtml(l.href)}" target="_blank" rel="noopener noreferrer">${escapeHtml(l.href)}</a>`)
      .join("<br/>");
    const shotName = entry.screenshot_path ? entry.screenshot_path.split("/").pop() : "";
    const shotHtml = shotName
      ? `<div class="preview-box"><strong>${escapeHtml(t("missed_snap.screenshot"))}</strong><br/><img src="/api/screenshots/${encodeURIComponent(shotName)}" style="max-width:100%;border-radius:6px;margin-top:6px;" /></div>`
      : "";
    const finalUrlHtml = isSafeHttpUrl(entry.final_url)
      ? `<br/><strong>${escapeHtml(t("missed_snap.target_url"))}</strong> <a href="${escapeHtml(entry.final_url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(entry.final_url)}</a>`
      : "";
    qs("snap-body").innerHTML = `
      <div class="preview-box"><strong>${escapeHtml(t("missed_snap.reason"))}</strong> ${escapeHtml(entry.failure_reason)}<br/><strong>${escapeHtml(t("missed_snap.mail_id"))}</strong> ${escapeHtml(entry.account)}/${escapeHtml(entry.mailbox)} #${escapeHtml(entry.internal_id)}${finalUrlHtml}<div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap;align-items:center;"><button class="btn small primary" data-missed-mark-unsub="${entry.id}">${escapeHtml(t("proof.mark_unsub_archive"))}</button><button class="btn small secondary" data-missed-copy-report="${entry.id}">${escapeHtml(t("missed_snap.copy_report"))}</button><span id="missed-copy-status" style="font-size:11px;color:var(--text-secondary);"></span></div></div>
      <div id="missed-redacted-preview" class="preview-box hidden"></div>
      ${shotHtml}
      <div class="preview-box"><strong>${escapeHtml(t("missed_snap.candidate_links"))}</strong><br/>${links || escapeHtml(t("missed_snap.none"))}</div>
      <div class="preview-box"><strong>${escapeHtml(t("missed_snap.headers"))}</strong><pre>${escapeHtml(entry.headers_snapshot)}</pre></div>
      <div class="preview-box"><strong>${escapeHtml(t("missed_snap.footer"))}</strong><pre>${escapeHtml(entry.footer_html_snapshot)}</pre></div>
    `;
    qs("snapshot-modal").classList.remove("hidden");
  }

  function showQuickGuideModal() {
    qs("snap-title").textContent = t("guide.title");
    qs("snap-body").innerHTML = `
      <div class="preview-box">
        <strong>${escapeHtml(t("guide.archive_title"))}</strong>
        <p style="margin-top:4px;line-height:1.45;">${escapeHtml(t("guide.archive_body"))}</p>
      </div>
      <div class="preview-box">
        <strong>${escapeHtml(t("guide.concepts_title"))}</strong>
        <ul class="guide-list" style="margin-top:6px;">
          <li>${t("guide.item_tray")}</li>
          <li>${t("guide.item_catchup")}</li>
          <li>${t("guide.item_missed")}</li>
          <li>${t("guide.item_browser")}</li>
          <li>${t("guide.item_remember")}</li>
        </ul>
      </div>
    `;
    qs("snapshot-modal").classList.remove("hidden");
  }

  function parseCsvOrLines(raw) {
    return String(raw || "")
      .split(/[\n,]+/)
      .map((s) => s.trim())
      .filter(Boolean);
  }

  function parseArchiveMapLines(raw) {
    const map = {};
    for (const line of String(raw || "").split("\n")) {
      const idx = line.indexOf("=");
      if (idx === -1) continue;
      const acct = line.slice(0, idx).trim();
      const box = line.slice(idx + 1).trim();
      if (acct && box) map[acct] = box;
    }
    return map;
  }

  async function openSettingsModal() {
    const cfg = await api("/api/settings");
    const u = cfg.user || {};
    const a = cfg.accounts || {};
    const r = cfg.rules || {};
    const b = cfg.browser || {};
    const c = cfg.consulting || {};
    const p = cfg.profile || {};

    const stratOpts = (a.valid_gmail_strategies || ["ui_menu", "two_hop", "tag_staging"])
      .map((s) => `<option value="${escapeHtml(s)}" ${a.gmail_archive_strategy === s ? "selected" : ""}>${escapeHtml(s)}</option>`)
      .join("");
    const packChecks = (r.available_packs || [])
      .map((pk) => {
        const checked = (r.enabled_packs || []).includes(pk.name) ? "checked" : "";
        return `<label class="toggle-check" title="${escapeHtml(pk.description)}">
          <input type="checkbox" class="cfg-pack-cb" value="${escapeHtml(pk.name)}" ${checked} />
          <span>${escapeHtml(pk.name)}: ${escapeHtml(pk.description)}</span>
        </label>`;
      })
      .join("");
    const archLines = Object.entries(a.archive_map || {})
      .map(([acct, box]) => `${acct} = ${box}`)
      .join("\n");
    const acctPlaceholder = (a.known_accounts || []).length
      ? (a.known_accounts || []).map((k) => `${k} = Archive`).join("\n")
      : "iCloud = Archive";

    const uiLangs = u.available_ui_languages || state.availableLocales || [{ code: "en", name: "English" }];
    const currentUiLang = String(u.ui_language || "");
    const uiLangOpts = [
      `<option value="" ${currentUiLang === "" ? "selected" : ""}>${escapeHtml(t("settings.ui_language_auto"))}</option>`,
      ...uiLangs.map(
        (loc) =>
          `<option value="${escapeHtml(loc.code)}" ${currentUiLang === loc.code ? "selected" : ""}>${escapeHtml(loc.name)} (${escapeHtml(loc.code)})</option>`
      ),
    ].join("");

    qs("snap-title").textContent = t("settings.title");
    qs("snap-body").innerHTML = `
      <div style="font-size:11px;color:var(--text-secondary);">${escapeHtml(t("settings.config_file"))} <code>${escapeHtml(cfg.config_path || "")}</code></div>
      <div class="preview-box" style="max-height:none;">
        <strong>${escapeHtml(t("settings.section_scan"))}</strong>
        <div class="settings-grid" style="margin-top:8px;">
          <div class="settings-field">
            <label for="cfg-ui-language">${escapeHtml(t("settings.ui_language"))}</label>
            <select id="cfg-ui-language">${uiLangOpts}</select>
          </div>
          <div class="settings-field">
            <label for="cfg-max-inbox">${escapeHtml(t("settings.max_inbox"))}</label>
            <input id="cfg-max-inbox" type="number" min="1" max="5000" value="${Number(u.max_inbox_messages || 500)}" />
          </div>
          <div class="settings-field">
            <label for="cfg-catchup-limit">${escapeHtml(t("settings.catchup_limit"))}</label>
            <input id="cfg-catchup-limit" type="number" min="1" max="5000" value="${Number(u.catchup_scan_limit || 200)}" />
          </div>
          <div class="settings-field">
            <label for="cfg-catchup-days">${escapeHtml(t("settings.catchup_days"))}</label>
            <input id="cfg-catchup-days" type="number" min="1" max="365" value="${Number(u.catchup_days_back || 14)}" />
          </div>
          <div class="settings-field">
            <label for="cfg-gmail-strategy">${escapeHtml(t("settings.gmail_strategy"))}</label>
            <select id="cfg-gmail-strategy">${stratOpts}</select>
          </div>
          <div class="settings-field">
            <label for="cfg-idle-timeout">${escapeHtml(t("settings.idle_timeout"))}</label>
            <input id="cfg-idle-timeout" type="number" min="0" max="1440" value="${Number(u.idle_timeout_minutes ?? 45)}" />
          </div>
        </div>
        <div style="margin-top:8px;">
          <label class="toggle-check">
            <input type="checkbox" id="cfg-mark-read" ${u.mark_read_on_archive !== false ? "checked" : ""} />
            <span>${escapeHtml(t("settings.mark_read"))}</span>
          </label>
          <label class="toggle-check" style="margin-top:6px;">
            <input type="checkbox" id="cfg-check-updates" ${u.check_updates !== false ? "checked" : ""} />
            <span>${escapeHtml(t("settings.check_updates"))}</span>
          </label>
        </div>
        <div class="settings-field" style="margin-top:8px;">
          <label for="cfg-archive-map">${t("settings.archive_map")}</label>
          <textarea id="cfg-archive-map" placeholder="${escapeHtml(acctPlaceholder)}">${escapeHtml(archLines)}</textarea>
        </div>
      </div>

      <div class="preview-box" style="max-height:none;">
        <strong>${escapeHtml(t("settings.section_rules"))}</strong>
        <div style="margin-top:6px;display:flex;flex-direction:column;gap:4px;">${packChecks || `<em>${escapeHtml(t("settings.no_packs"))}</em>`}</div>
        <div class="settings-grid" style="margin-top:8px;">
          <div class="settings-field">
            <label for="cfg-wanted-domains">${escapeHtml(t("settings.wanted_domains"))}</label>
            <textarea id="cfg-wanted-domains">${escapeHtml((p.wanted_domains || []).join(", "))}</textarea>
          </div>
          <div class="settings-field">
            <label for="cfg-bill-keywords">${escapeHtml(t("settings.bill_keywords"))}</label>
            <textarea id="cfg-bill-keywords">${escapeHtml((p.bill_keywords || []).join(", "))}</textarea>
          </div>
          <div class="settings-field">
            <label for="cfg-transactional-domains">${escapeHtml(t("settings.transactional_domains"))}</label>
            <textarea id="cfg-transactional-domains">${escapeHtml((p.transactional_domains || []).join(", "))}</textarea>
          </div>
          <div class="settings-field">
            <label for="cfg-browser-locale">${escapeHtml(t("settings.browser_locale", { effective: b.effective_locale || "en-US" }))}</label>
            <input id="cfg-browser-locale" type="text" value="${escapeHtml(b.locale || "")}" placeholder="${escapeHtml(t("settings.browser_locale_placeholder"))}" />
            <label for="cfg-accept-lang" style="margin-top:4px;">${escapeHtml(t("settings.accept_lang"))}</label>
            <input id="cfg-accept-lang" type="text" value="${escapeHtml(b.accept_language || "")}" placeholder="${escapeHtml(b.effective_accept_language || "en-US,en;q=0.9")}" />
          </div>
        </div>
      </div>

      <div class="preview-box" style="max-height:none;">
        <strong>${escapeHtml(t("settings.section_consulting"))}</strong>
        <div style="margin-top:6px;">
          <label class="toggle-check">
            <input type="checkbox" id="cfg-consult-enabled" ${c.enabled ? "checked" : ""} />
            <span>${escapeHtml(t("settings.consulting_enable"))}</span>
          </label>
        </div>
        <div class="settings-grid" style="margin-top:8px;">
          <div class="settings-field">
            <label for="cfg-consult-networks">${escapeHtml(t("settings.consulting_networks"))}</label>
            <textarea id="cfg-consult-networks">${escapeHtml((c.networks || []).join(", "))}</textarea>
          </div>
          <div class="settings-field">
            <label for="cfg-consult-match">${escapeHtml(t("settings.consulting_match"))}</label>
            <textarea id="cfg-consult-match">${escapeHtml((c.match_keywords || []).join(", "))}</textarea>
          </div>
          <div class="settings-field">
            <label for="cfg-consult-negative">${escapeHtml(t("settings.consulting_negative"))}</label>
            <textarea id="cfg-consult-negative">${escapeHtml((c.negative_keywords || []).join(", "))}</textarea>
          </div>
          <div class="settings-field">
            <label for="cfg-consult-boilerplate">${escapeHtml(t("settings.consulting_boilerplate"))}</label>
            <textarea id="cfg-consult-boilerplate">${escapeHtml(c.decline_boilerplate || "")}</textarea>
          </div>
        </div>
      </div>

      <div class="settings-footer">
        <span id="settings-status" class="modal-status">${escapeHtml(t("settings.status_hint"))}</span>
        <button class="btn small primary" id="btn-save-settings">${escapeHtml(t("settings.save_btn"))}</button>
      </div>
    `;
    qs("snapshot-modal").classList.remove("hidden");
  }

  function formatSenderRuleStatus(row) {
    const tags = [];
    if (row.preferred_action === "always_keep") tags.push(`<span class="pill green">${escapeHtml(t("rules.always_keep"))}</span>`);
    else if (row.preferred_action === "archive") tags.push(`<span class="pill accent">${escapeHtml(t("rules.always_archive"))}</span>`);
    else if (row.preferred_action === "mark_read") tags.push(`<span class="pill muted">${escapeHtml(t("rules.always_mark_read"))}</span>`);
    if (row.unsubscribed_at || row.unsub_count > 0) {
      tags.push(`<span class="pill warn">${escapeHtml(t("rules.unsub_badge", { count: Number(row.unsub_count || 0) }))}</span>`);
    }
    if (!tags.length) tags.push(`<span class="pill muted">${escapeHtml(t("rules.seen_badge"))}</span>`);
    return tags.join(" ");
  }

  async function refreshSenderRulesData({ rowsOnly = false } = {}) {
    const seq = ++state.rulesSeq;
    const params = new URLSearchParams({
      search: state.rulesSearch || "",
      kind: state.rulesFilter || "all",
    });
    const res = await api(`/api/sender-memory?${params.toString()}`);
    if (seq !== state.rulesSeq) return;
    state.rulesItems = res.items || [];
    const valid = new Set(state.rulesItems.map((r) => r.sender_key));
    state.rulesSelectedKeys = new Set([...state.rulesSelectedKeys].filter((k) => valid.has(k)));
    if (rowsOnly && qs("rules-tbody")) {
      renderSenderRulesRowsOnly();
    } else {
      renderSenderRulesModalBody();
    }
  }

  function renderSenderRulesRowsHtml(items) {
    return items.length
      ? items
          .map((r) => {
            const chk = state.rulesSelectedKeys.has(r.sender_key) ? "checked" : "";
            return `<tr>
              <td style="width:28px;"><input type="checkbox" class="rules-row-cb" data-sender-key="${escapeHtml(r.sender_key)}" ${chk} /></td>
              <td>
                <strong>${escapeHtml(r.sender_key)}</strong>
                ${r.display_name ? `<div class="sub-meta">${escapeHtml(r.display_name)}</div>` : ""}
              </td>
              <td>${formatSenderRuleStatus(r)}</td>
              <td style="font-size:11px;color:var(--text-secondary);">${escapeHtml((r.last_seen_at || "").slice(0, 16).replace("T", " "))}</td>
              <td>
                <select class="pref-select rule-edit-select" data-rule-key="${escapeHtml(r.sender_key)}">
                  <option value="">${escapeHtml(t("rules.change_placeholder"))}</option>
                  <option value="always_keep">${escapeHtml(t("rules.always_keep"))}</option>
                  <option value="archive">${escapeHtml(t("rules.always_archive"))}</option>
                  <option value="mark_read">${escapeHtml(t("rules.always_mark_read"))}</option>
                  <option value="clear">${escapeHtml(t("pref.clear"))}</option>
                  <option value="delete">${escapeHtml(t("rules.delete_entry"))}</option>
                </select>
              </td>
            </tr>`;
          })
          .join("")
      : `<tr><td colspan="5" style="text-align:center;color:var(--text-secondary);padding:16px;">${escapeHtml(t("rules.empty"))}</td></tr>`;
  }

  function renderSenderRulesRowsOnly() {
    const items = state.rulesItems || [];
    const allSel = items.length > 0 && items.every((r) => state.rulesSelectedKeys.has(r.sender_key));
    qs("snap-title").textContent = t("rules.title", { count: items.length });
    const tbody = qs("rules-tbody");
    if (tbody) tbody.innerHTML = renderSenderRulesRowsHtml(items);
    const mcb = qs("rules-master-cb");
    if (mcb instanceof HTMLInputElement) mcb.checked = allSel;
    const bulkBtn = qs("btn-rules-bulk-apply");
    if (bulkBtn instanceof HTMLButtonElement) bulkBtn.disabled = state.rulesSelectedKeys.size === 0;
    const bulkSel = qs("rules-bulk-action");
    if (bulkSel instanceof HTMLSelectElement && bulkSel.options.length > 0) {
      bulkSel.options[0].textContent = t("rules.bulk_placeholder", { count: state.rulesSelectedKeys.size });
    }
  }

  function renderSenderRulesModalBody() {
    const kinds = [
      { id: "all", label: t("rules.all") },
      { id: "always_keep", label: t("rules.always_keep") },
      { id: "archive", label: t("rules.always_archive") },
      { id: "mark_read", label: t("rules.always_mark_read") },
      { id: "unsubscribed", label: t("rules.unsubscribed") },
    ];
    const filterHtml = kinds
      .map(
        (k) =>
          `<button class="tab ${state.rulesFilter === k.id ? "active" : ""}" data-rules-kind="${k.id}">${escapeHtml(k.label)}</button>`
      )
      .join("");
    const items = state.rulesItems || [];
    const allSel = items.length > 0 && items.every((r) => state.rulesSelectedKeys.has(r.sender_key));
    const rowsHtml = renderSenderRulesRowsHtml(items);

    qs("snap-title").textContent = t("rules.title", { count: items.length });
    qs("snap-body").innerHTML = `
      <div class="rules-toolbar">
        <div class="tabs">${filterHtml}</div>
        <input type="search" id="rules-search-input" class="search-box" value="${escapeHtml(state.rulesSearch)}" placeholder="${escapeHtml(t("rules.search_placeholder"))}" />
      </div>
      <div class="rules-toolbar">
        <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;">
          <select id="rules-bulk-action" class="pref-select" style="font-size:12px;padding:4px 8px;">
            <option value="">${escapeHtml(t("rules.bulk_placeholder", { count: state.rulesSelectedKeys.size }))}</option>
            <option value="always_keep">${escapeHtml(t("rules.bulk_keep"))}</option>
            <option value="archive">${escapeHtml(t("rules.bulk_archive"))}</option>
            <option value="mark_read">${escapeHtml(t("rules.bulk_mark_read"))}</option>
            <option value="clear">${escapeHtml(t("rules.bulk_clear"))}</option>
            <option value="delete">${escapeHtml(t("rules.bulk_delete"))}</option>
          </select>
          <button class="btn small secondary" id="btn-rules-bulk-apply" ${state.rulesSelectedKeys.size === 0 ? "disabled" : ""}>${escapeHtml(t("rules.apply_bulk"))}</button>
        </div>
        <span id="rules-status" class="modal-status"></span>
      </div>
      <div class="tbl-wrap">
        <table class="tbl">
          <thead>
            <tr>
              <th><input type="checkbox" id="rules-master-cb" ${allSel ? "checked" : ""} /></th>
              <th>${escapeHtml(t("rules.col_sender"))}</th>
              <th>${escapeHtml(t("rules.col_history"))}</th>
              <th>${escapeHtml(t("rules.col_last_seen"))}</th>
              <th>${escapeHtml(t("rules.col_edit"))}</th>
            </tr>
          </thead>
          <tbody id="rules-tbody">${rowsHtml}</tbody>
        </table>
      </div>
    `;
    qs("snapshot-modal").classList.remove("hidden");
  }

  async function openSenderRulesModal() {
    await refreshSenderRulesData();
  }

  async function applySenderMemoryUpdate(keys, action) {
    await api("/api/sender-memory/bulk", {
      method: "POST",
      body: JSON.stringify({ sender_keys: keys, action }),
    });
    const keySet = new Set(keys.map((k) => k.toLowerCase()));
    for (const item of state.data?.items || []) {
      const eml = (item.header?.sender_email || "").toLowerCase();
      const dom = (item.header?.sender_domain || "").toLowerCase();
      if (keySet.has(eml) || keySet.has(dom)) {
        state.actionsByUid.delete(item.uid);
      }
    }
    await refreshSenderRulesData();
    await loadState(false);
  }

  async function refreshAuditLogData({ rowsOnly = false } = {}) {
    const seq = ++state.auditSeq;
    const params = new URLSearchParams({
      status: state.auditStatus || "all",
      run_id: state.auditRunId || "",
      search: state.auditSearch || "",
      limit: "200",
    });
    const res = await api(`/api/audit-log?${params.toString()}`);
    if (seq !== state.auditSeq) return;
    state.auditItems = res.items || [];
    state.auditRuns = res.runs || [];
    if (state.auditRunId && !state.auditRuns.some((r) => r.run_id === state.auditRunId)) {
      state.auditRunId = "";
    }
    if (rowsOnly && qs("audit-tbody")) {
      renderAuditLogRowsOnly();
    } else {
      renderAuditLogModalBody();
    }
  }

  function renderAuditLogRowsHtml(items) {
    return items.length
      ? items
          .map((row) => {
            const st = String(row.status || "");
            const stCls =
              st === "ok" || st === "drafted"
                ? "green"
                : st === "blocked_captcha"
                ? "warn"
                : st === "failed"
                ? "danger"
                : "muted";
            const undoneTag = row.undone_at
              ? ` <span class="pill accent">${escapeHtml(t("audit.restored_badge", { ts: String(row.undone_at).slice(0, 16).replace("T", " ") }))}</span>`
              : "";
            const undoBtn = row.can_undo_archive
              ? `<button class="btn small secondary" data-audit-undo="${row.id}">${escapeHtml(t("audit.undo_btn"))}</button>`
              : "";
            const actLabel = actionLabel(row.action);
            return `<tr>
              <td style="font-size:11px;color:var(--text-secondary);white-space:nowrap;">${escapeHtml((row.created_at || "").slice(0, 16).replace("T", " "))}</td>
              <td><span class="acct ${accountToneClass(row.account)}">${escapeHtml(row.account)}</span></td>
              <td><span class="pill ${stCls}">${escapeHtml(st)}</span> <span class="pill muted">${escapeHtml(actLabel)}</span></td>
              <td>
                <strong>${escapeHtml(row.subject || t("row.no_subject"))}</strong>
                <div class="sub-meta">
                  <span>${escapeHtml(row.sender)}</span>
                  <span>· ${tHtml("audit.run_prefix", { id: (row.run_id || "").slice(0, 14) })}</span>
                  <span>· ${escapeHtml(row.detail)}</span>
                  ${undoneTag}
                </div>
              </td>
              <td>${undoBtn}</td>
            </tr>`;
          })
          .join("")
      : `<tr><td colspan="5" style="text-align:center;color:var(--text-secondary);padding:16px;">${escapeHtml(t("audit.empty"))}</td></tr>`;
  }

  function renderAuditLogRowsOnly() {
    const items = state.auditItems || [];
    qs("snap-title").textContent = t("audit.title", { count: items.length });
    const tbody = qs("audit-tbody");
    if (tbody) tbody.innerHTML = renderAuditLogRowsHtml(items);
  }

  function renderAuditLogModalBody() {
    const statuses = [
      { id: "all", label: t("audit.all") },
      { id: "ok", label: "ok" },
      { id: "failed", label: "failed" },
      { id: "blocked_captcha", label: "blocked_captcha" },
      { id: "drafted", label: "drafted" },
    ];
    const statusTabs = statuses
      .map(
        (s) =>
          `<button class="tab ${state.auditStatus === s.id ? "active" : ""}" data-audit-status="${escapeHtml(s.id)}">${escapeHtml(s.label)}</button>`
      )
      .join("");
    const runOpts = [
      `<option value="">${escapeHtml(t("audit.all_batches", { count: (state.auditRuns || []).length }))}</option>`,
      ...(state.auditRuns || []).map((r) => {
        const ts = String(r.created_at || "").slice(0, 16).replace("T", " ");
        const lbl = t("audit.batch_option", { run_id: r.run_id, ts, total: r.total, ok: r.ok, failed: r.failed });
        const sel = state.auditRunId === r.run_id ? "selected" : "";
        return `<option value="${escapeHtml(r.run_id)}" ${sel}>${escapeHtml(lbl)}</option>`;
      }),
    ].join("");
    const items = state.auditItems || [];
    qs("snap-title").textContent = t("audit.title", { count: items.length });
    qs("snap-body").innerHTML = `
      <div class="rules-toolbar">
        <div class="tabs">${statusTabs}</div>
        <input type="search" id="audit-search-input" class="search-box" value="${escapeHtml(state.auditSearch)}" placeholder="${escapeHtml(t("audit.search_placeholder"))}" />
      </div>
      <div class="rules-toolbar">
        <select id="audit-run-select" class="pref-select" style="font-size:12px;padding:4px 8px;">${runOpts}</select>
        <span id="audit-status" class="modal-status">${escapeHtml(t("audit.hint"))}</span>
      </div>
      <div class="tbl-wrap">
        <table class="tbl">
          <thead>
            <tr>
              <th>${escapeHtml(t("audit.col_time"))}</th>
              <th>${escapeHtml(t("audit.col_acct"))}</th>
              <th>${escapeHtml(t("audit.col_outcome"))}</th>
              <th>${escapeHtml(t("audit.col_detail"))}</th>
              <th>${escapeHtml(t("audit.col_restore"))}</th>
            </tr>
          </thead>
          <tbody id="audit-tbody">${renderAuditLogRowsHtml(items)}</tbody>
        </table>
      </div>
    `;
    qs("snapshot-modal").classList.remove("hidden");
  }

  async function openAuditLogModal() {
    state.auditRunId = "";
    await refreshAuditLogData();
  }

  document.addEventListener("click", async (ev) => {
    const target = ev.target;
    if (!(target instanceof HTMLElement)) return;
    try {
      if (target.id === "btn-save-settings") {
        const statusEl = qs("settings-status");
        const saveBtn = target instanceof HTMLButtonElement ? target : null;
        if (saveBtn) saveBtn.disabled = true;
        if (statusEl) {
          statusEl.className = "modal-status";
          statusEl.textContent = t("settings.saving");
        }
        try {
          const packs = [...document.querySelectorAll(".cfg-pack-cb:checked")].map((el) => el.value);
          const payload = {
            user: {
              ui_language: (qs("cfg-ui-language")?.value || "").trim(),
              check_updates: Boolean(qs("cfg-check-updates")?.checked),
              mark_read_on_archive: Boolean(qs("cfg-mark-read")?.checked),
              max_inbox_messages: Number(qs("cfg-max-inbox")?.value || 500),
              catchup_scan_limit: Number(qs("cfg-catchup-limit")?.value || 200),
              catchup_days_back: Number(qs("cfg-catchup-days")?.value || 14),
              idle_timeout_minutes: Number(qs("cfg-idle-timeout")?.value ?? 45),
            },
            accounts: {
              gmail_archive_strategy: qs("cfg-gmail-strategy")?.value || "ui_menu",
              archive_map: parseArchiveMapLines(qs("cfg-archive-map")?.value || ""),
            },
            rules: {
              enabled_packs: packs,
            },
            browser: {
              locale: (qs("cfg-browser-locale")?.value || "").trim(),
              accept_language: (qs("cfg-accept-lang")?.value || "").trim(),
            },
            consulting: {
              enabled: Boolean(qs("cfg-consult-enabled")?.checked),
              networks: parseCsvOrLines(qs("cfg-consult-networks")?.value || ""),
              match_keywords: parseCsvOrLines(qs("cfg-consult-match")?.value || ""),
              negative_keywords: parseCsvOrLines(qs("cfg-consult-negative")?.value || ""),
              decline_boilerplate: qs("cfg-consult-boilerplate")?.value || "",
            },
            profile: {
              wanted_domains: parseCsvOrLines(qs("cfg-wanted-domains")?.value || ""),
              bill_keywords: parseCsvOrLines(qs("cfg-bill-keywords")?.value || ""),
              transactional_domains: parseCsvOrLines(qs("cfg-transactional-domains")?.value || ""),
            },
          };
          await api("/api/settings", { method: "PUT", body: JSON.stringify(payload) });
          await loadI18n();
          checkForUpdates(false);
          state.actionsByUid.clear();
          qs("snapshot-modal").classList.add("hidden");
          await loadState(true);
        } catch (saveErr) {
          if (statusEl) {
            statusEl.className = "modal-status err";
            statusEl.textContent = t("settings.save_failed", { message: saveErr.message });
          }
        } finally {
          if (saveBtn) saveBtn.disabled = false;
        }
        return;
      }
      if (target.dataset.rulesKind) {
        state.rulesFilter = target.dataset.rulesKind;
        await refreshSenderRulesData();
        return;
      }
      if (target.dataset.auditStatus) {
        state.auditStatus = target.dataset.auditStatus;
        await refreshAuditLogData();
        return;
      }
      if (target.dataset.auditUndo) {
        const undoTriggerBtn = target instanceof HTMLButtonElement ? target : null;
        const entryId = Number(target.dataset.auditUndo);
        const row = (state.auditItems || []).find((r) => r.id === entryId);
        qs("modal-title").textContent = t("audit.confirm_title");
        qs("modal-body").innerHTML = row
          ? `<div class="confirm-warning warn">${tHtml("audit.confirm_body", {
              subject: row.subject || t("row.no_subject"),
              sender: row.sender,
              account: row.account,
            })}</div>`
          : `<div class="confirm-warning warn">${t("audit.confirm_body_generic")}</div>`;
        qs("modal-confirm").disabled = false;
        qs("confirm-modal").classList.remove("hidden");
        state.confirmCallback = async () => {
          qs("confirm-modal").classList.add("hidden");
          if (undoTriggerBtn) undoTriggerBtn.disabled = true;
          const statusEl = qs("audit-status");
          let resp;
          try {
            resp = await api(`/api/audit-log/${entryId}/undo`, { method: "POST" });
          } catch (undoErr) {
            if (undoTriggerBtn) undoTriggerBtn.disabled = false;
            if (statusEl) {
              statusEl.className = "modal-status err";
              statusEl.textContent = t("audit.undo_failed", { message: undoErr.message });
            } else {
              showError(undoErr.message);
            }
            return;
          }
          if (row) {
            for (const item of state.data?.items || []) {
              if (item.header?.account === row.account && String(item.header?.internal_id) === String(row.internal_id)) {
                state.actionsByUid.delete(item.uid);
              }
            }
          }
          await refreshAuditLogData();
          const nextEl = qs("audit-status");
          if (nextEl) {
            nextEl.className = "modal-status ok";
            nextEl.textContent = t("audit.restored_ok", { dest: resp.restored_to || "INBOX" });
          }
          try {
            await loadState(false);
          } catch (scanErr) {
            showError(scanErr.message);
          }
        };
        return;
      }
      if (target.id === "btn-copy-upgrade-cmd" && target.dataset.upgradeCmd) {
        const cmd = target.dataset.upgradeCmd;
        try {
          if (navigator.clipboard && navigator.clipboard.writeText) {
            await navigator.clipboard.writeText(cmd);
            target.textContent = t("update.copied");
          }
        } catch (_clipErr) {
          // Clipboard permission denied; command remains visible in the banner
        }
        return;
      }
      if (target.id === "btn-dismiss-update" && target.dataset.dismissVer) {
        try {
          if (window.localStorage) {
            window.localStorage.setItem("nollpost_dismissed_update", target.dataset.dismissVer);
          }
        } catch (_ignored) {
          // Ignore storage errors
        }
        renderUpdateBanner();
        return;
      }
      if (target.id === "btn-rules-bulk-apply") {
        const actionEl = qs("rules-bulk-action");
        const act = actionEl instanceof HTMLSelectElement ? actionEl.value : "";
        const keys = [...state.rulesSelectedKeys];
        const statusEl = qs("rules-status");
        if (!act || !keys.length) return;
        const runBulk = async () => {
          await applySenderMemoryUpdate(keys, act);
          const nextStatus = qs("rules-status");
          if (nextStatus) {
            nextStatus.className = "modal-status ok";
            nextStatus.textContent = t("rules.updated_bulk", { count: keys.length });
          }
        };
        if (act === "delete") {
          qs("modal-title").textContent = t("rules.confirm_delete_title", { count: keys.length });
          qs("modal-body").innerHTML = `<div class="confirm-warning warn">${tHtml("rules.confirm_delete_body", { count: keys.length })}</div>`;
          qs("modal-confirm").disabled = false;
          qs("confirm-modal").classList.remove("hidden");
          state.confirmCallback = async () => {
            qs("confirm-modal").classList.add("hidden");
            try {
              await runBulk();
            } catch (bulkErr) {
              const errStatus = qs("rules-status");
              if (errStatus) {
                errStatus.className = "modal-status err";
                errStatus.textContent = bulkErr.message;
              } else {
                showError(bulkErr.message);
              }
            }
          };
        } else {
          try {
            await runBulk();
          } catch (bulkErr) {
            if (statusEl) {
              statusEl.className = "modal-status err";
              statusEl.textContent = bulkErr.message;
            }
          }
        }
        return;
      }
      if (target.id === "btn-reset-filters") {
        state.category = "all";
        state.account = "all";
        state.unreadOnly = false;
        state.search = "";
        qs("unread-only-toggle").checked = false;
        qs("search-input").value = "";
        render();
        return;
      }
      if (target.dataset.retryScan) {
        await loadState(true);
        return;
      }
      if (target.dataset.trayDismissUid) {
        const uid = target.dataset.trayDismissUid;
        if (target instanceof HTMLButtonElement) target.disabled = true;
        qs("modal-confirm").disabled = true;
        state.confirmCallback = null;
        try {
          await api("/api/tray/dismiss", {
            method: "POST",
            body: JSON.stringify({ uids: [uid] }),
          });
          await loadState(false);
          if ((state.data?.tray || []).length > 0) {
            await openTrayConfirmation();
          } else {
            qs("confirm-modal").classList.add("hidden");
          }
        } finally {
          if (target instanceof HTMLButtonElement) target.disabled = false;
          qs("modal-confirm").disabled = false;
        }
        return;
      }
      if (target.dataset.openUrl && isSafeHttpUrl(target.dataset.openUrl)) {
        window.open(target.dataset.openUrl, "_blank", "noopener,noreferrer");
        return;
      }
      if (target.dataset.cat) {
        state.category = target.dataset.cat;
        render();
        return;
      }
      if (target.dataset.acct) {
        state.account = target.dataset.acct;
        render();
        return;
      }
      if (target.dataset.mode) {
        state.mode = target.dataset.mode;
        state.category = "all";
        await loadState(false);
        return;
      }
      if (target.dataset.uid && target.dataset.act) {
        state.actionsByUid.set(target.dataset.uid, target.dataset.act);
        render();
        return;
      }
      if (target.dataset.enqueueMissed) {
        await api("/api/missed/enqueue", {
          method: "POST",
          body: JSON.stringify({ uid: target.dataset.enqueueMissed }),
        });
        await loadState(false);
        return;
      }
      if (target.dataset.proofIdx !== undefined) {
        showProofModal(Number(target.dataset.proofIdx));
        return;
      }
      if (target.dataset.missedInspect) {
        showMissedSnapshot(Number(target.dataset.missedInspect));
        return;
      }
      if (target.dataset.missedCopyReport) {
        const mid = Number(target.dataset.missedCopyReport);
        const statusEl = qs("missed-copy-status");
        const prevEl = qs("missed-redacted-preview");
        let rep;
        try {
          rep = await api(`/api/missed/${mid}/export`);
        } catch (copyErr) {
          if (statusEl) statusEl.textContent = t("missed_snap.export_failed", { message: copyErr.message });
          return;
        }
        if (prevEl) {
          prevEl.classList.remove("hidden");
          prevEl.innerHTML = `<strong>${escapeHtml(t("missed_snap.preview_title"))}</strong><pre style="margin-top:6px;">${escapeHtml(rep.markdown)}</pre>`;
        }
        try {
          if (navigator.clipboard && navigator.clipboard.writeText) {
            await navigator.clipboard.writeText(rep.markdown);
            if (statusEl) statusEl.textContent = t("missed_snap.copied");
          } else if (statusEl) {
            statusEl.textContent = t("missed_snap.clip_unavailable");
          }
        } catch (_clipErr) {
          if (statusEl) statusEl.textContent = t("missed_snap.clip_denied");
        }
        return;
      }
      if (target.dataset.missedOpen && isSafeHttpUrl(target.dataset.missedOpen)) {
        window.open(target.dataset.missedOpen, "_blank", "noopener,noreferrer");
        return;
      }
      if (target.dataset.markUnsubIdx !== undefined) {
        const idx = Number(target.dataset.markUnsubIdx);
        const r = state.lastJobResults[idx];
        if (!r) return;
        const resp = await api("/api/unsub/mark-succeeded", {
          method: "POST",
          body: JSON.stringify({
            uid: r.uid || "",
            account: r.account || "",
            internal_id: r.internal_id || "",
            sender: r.sender || "",
            subject: r.subject || "",
            archive: r.action !== "unsub_only",
          }),
        });
        r.status = "ok";
        r.detail = resp.archived
          ? t("results.marked_unsub_archived")
          : t("results.marked_unsub");
        qs("snapshot-modal").classList.add("hidden");
        renderJobResults(state.lastJobResults);
        await loadState(false);
        return;
      }
      if (target.dataset.missedMarkUnsub) {
        const mid = Number(target.dataset.missedMarkUnsub);
        const resp = await api("/api/unsub/mark-succeeded", {
          method: "POST",
          body: JSON.stringify({ missed_id: mid, archive: true }),
        });
        qs("snapshot-modal").classList.add("hidden");
        renderJobResults([
          {
            sender: t("results.missed_label", { id: mid }),
            action: "archive_and_unsub",
            status: "ok",
            detail: resp.archived
              ? t("results.marked_unsub_archived")
              : t("results.marked_unsub"),
          },
        ]);
        await loadState(false);
        return;
      }
      if (target.dataset.missedResolve) {
        await api(`/api/missed/${Number(target.dataset.missedResolve)}/resolve`, {
          method: "POST",
          body: JSON.stringify({ note: "Resolved from UI" }),
        });
        await loadState(false);
        return;
      }
      if (target.dataset.missedRetry) {
        const res = await api(`/api/missed/${Number(target.dataset.missedRetry)}/retry`, {
          method: "POST",
          body: JSON.stringify({ headless: !qs("show-browser-toggle").checked }),
        });
        renderJobResults([
          {
            sender: t("results.missed_label", { id: target.dataset.missedRetry }),
            status: res.outcome.status === "success" ? "ok" : res.outcome.status,
            detail: res.outcome.detail,
            opened_target: res.outcome.final_url,
          },
        ]);
        await loadState(false);
        return;
      }
    } catch (err) {
      showError(err.message);
    }
  });

  document.addEventListener("change", async (ev) => {
    const target = ev.target;
    if (!(target instanceof HTMLElement)) return;
    try {
      if (target.id === "master-cb" && target instanceof HTMLInputElement) {
        for (const item of filteredItems()) {
          if (target.checked) state.selectedUids.add(item.uid);
          else state.selectedUids.delete(item.uid);
        }
        render();
        return;
      }
      if (target.classList.contains("row-cb") && target instanceof HTMLInputElement && target.dataset.uid) {
        if (target.checked) state.selectedUids.add(target.dataset.uid);
        else state.selectedUids.delete(target.dataset.uid);
        render();
        return;
      }
      if (target.id === "unread-only-toggle" && target instanceof HTMLInputElement) {
        state.unreadOnly = target.checked;
        render();
        return;
      }
      if (target.classList.contains("pref-select") && target instanceof HTMLSelectElement && target.value) {
        const targetEmail = (target.dataset.prefEmail || "").toLowerCase();
        await api("/api/preferences", {
          method: "POST",
          body: JSON.stringify({
            sender_email: target.dataset.prefEmail || "",
            sender_domain: target.dataset.prefDom || "",
            display_name: target.dataset.prefName || "",
            preference: target.value,
          }),
        });
        target.value = "";
        // Clear cached row actions for this sender so the updated default takes effect immediately
        for (const item of state.data?.items || []) {
          if (item.header.sender_email.toLowerCase() === targetEmail) {
            state.actionsByUid.delete(item.uid);
          }
        }
        await loadState(false);
        return;
      }
      if (target.id === "rules-master-cb" && target instanceof HTMLInputElement) {
        for (const row of state.rulesItems || []) {
          if (target.checked) state.rulesSelectedKeys.add(row.sender_key);
          else state.rulesSelectedKeys.delete(row.sender_key);
        }
        renderSenderRulesRowsOnly();
        return;
      }
      if (target.classList.contains("rules-row-cb") && target instanceof HTMLInputElement && target.dataset.senderKey) {
        if (target.checked) state.rulesSelectedKeys.add(target.dataset.senderKey);
        else state.rulesSelectedKeys.delete(target.dataset.senderKey);
        renderSenderRulesRowsOnly();
        return;
      }
      if (target.classList.contains("rule-edit-select") && target instanceof HTMLSelectElement && target.value && target.dataset.ruleKey) {
        const key = target.dataset.ruleKey;
        const act = target.value;
        target.value = "";
        await applySenderMemoryUpdate([key], act);
        const nextStatus = qs("rules-status");
        if (nextStatus) {
          nextStatus.className = "modal-status ok";
          nextStatus.textContent = t("rules.updated_one", { key });
        }
        return;
      }
      if (target.id === "audit-run-select" && target instanceof HTMLSelectElement) {
        state.auditRunId = target.value;
        await refreshAuditLogData({ rowsOnly: true });
        return;
      }
    } catch (err) {
      showError(err.message);
    }
  });

  document.addEventListener("input", async (ev) => {
    const target = ev.target;
    if (target instanceof HTMLInputElement && target.id === "rules-search-input") {
      state.rulesSearch = target.value;
      try {
        await refreshSenderRulesData({ rowsOnly: true });
      } catch (err) {
        showError(err.message);
      }
      return;
    }
    if (target instanceof HTMLInputElement && target.id === "audit-search-input") {
      state.auditSearch = target.value;
      try {
        await refreshAuditLogData({ rowsOnly: true });
      } catch (err) {
        showError(err.message);
      }
    }
  });

  qs("search-input").addEventListener("input", (ev) => {
    state.search = ev.target.value;
    render();
  });
  qs("refresh-btn").addEventListener("click", () => loadState(true).catch((e) => showError(e.message)));
  qs("sender-rules-btn").addEventListener("click", () => openSenderRulesModal().catch((e) => showError(e.message)));
  qs("audit-log-btn").addEventListener("click", () => openAuditLogModal().catch((e) => showError(e.message)));
  qs("settings-btn").addEventListener("click", () => openSettingsModal().catch((e) => showError(e.message)));
  qs("help-btn").addEventListener("click", () => showQuickGuideModal());
  qs("btn-reset-actions").addEventListener("click", () => {
    for (const item of filteredItems()) {
      state.actionsByUid.set(item.uid, "no_action");
    }
    render();
  });
  qs("btn-exec-selected").addEventListener("click", () => {
    const items = (state.data?.items || []).filter((i) => state.selectedUids.has(i.uid));
    openConfirmation(t("confirm.title_selected"), nonNoopPairs(items)).catch((e) => showError(e.message));
  });
  qs("btn-exec-category").addEventListener("click", () => {
    const catLabel = CATEGORY_IDS.includes(state.category)
      ? categoryLabel(state.category)
      : t("cat.category_fallback");
    openConfirmation(t("confirm.title_category", { category: catLabel }), nonNoopPairs(filteredItems())).catch((e) => showError(e.message));
  });
  qs("btn-exec-all").addEventListener("click", () => {
    openConfirmation(t("confirm.title_all"), nonNoopPairs(state.data?.items || [])).catch((e) => showError(e.message));
  });
  qs("btn-tray-sweep").addEventListener("click", () => openTrayConfirmation().catch((e) => showError(e.message)));
  qs("btn-tray-dismiss").addEventListener("click", () => {
    const tray = state.data?.tray || [];
    if (!tray.length) return;
    const uids = tray.map((item) => item.message_uid);
    qs("modal-title").textContent = t("confirm.tray_dismiss_title", { count: tray.length });
    qs("modal-body").innerHTML = `
      <div class="confirm-warning warn">
        ${tHtml("confirm.tray_dismiss_body", { count: tray.length })}
      </div>
    `;
    qs("modal-confirm").disabled = false;
    qs("confirm-modal").classList.remove("hidden");
    state.confirmCallback = async () => {
      qs("confirm-modal").classList.add("hidden");
      await api("/api/tray/dismiss", { method: "POST", body: JSON.stringify({ uids }) });
      await loadState(false);
    };
  });
  qs("modal-cancel").addEventListener("click", () => {
    state.confirmCallback = null;
    qs("confirm-modal").classList.add("hidden");
  });
  qs("modal-confirm").addEventListener("click", () => {
    const cb = state.confirmCallback;
    state.confirmCallback = null;
    if (cb) cb().catch((e) => showError(e.message));
  });
  qs("snap-close").addEventListener("click", () => qs("snapshot-modal").classList.add("hidden"));

  (async () => {
    try {
      await loadI18n();
    } catch (_i18nErr) {
      // Fall back to static English DOM strings if locale fetch fails
    }
    checkForUpdates(false);
    await loadState(false);
  })().catch((err) => {
    showError(err.message);
  });
})();
