(() => {
  const ACTION_LABELS = {
    no_action: "No action",
    mark_read: "Mark read",
    read_open_link: "Read + Open link",
    read_show_invoice: "Read + Show invoice",
    decline_and_archive: "Decline & Arch",
    draft_decline: "Draft Decline",
    send_decline_and_archive: "Send Decline & Arch",
    archive: "Archive",
    archive_and_unsub: "Archive & Unsub",
    unsub_only: "Unsub only",
  };

  const CATEGORIES = [
    { id: "all", label: "All" },
    { id: "attention", label: "Attention & Bills" },
    { id: "updates", label: "Updates & Comments" },
    { id: "consulting", label: "Consulting" },
    { id: "newsletters", label: "Newsletters & Bots" },
    { id: "missed", label: "Missed-Unsub Queue" },
  ];

  const state = {
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
    const title = perm
      ? "Mail.app Automation Permission Required"
      : "Could Not Scan Mail.app";
    const stepsHtml = perm
      ? `<p>macOS blocked AppleScript access to Mail.app. Grant permission and retry:</p>
         <ol style="margin-left:18px;line-height:1.5;">
           <li>Open <strong>System Settings → Privacy &amp; Security → Automation</strong>.</li>
           <li>Enable <strong>Mail</strong> under your terminal app (e.g. Terminal, iTerm2, Ghostty) or Python.</li>
           <li>Make sure Mail.app is open, or run <code>nollpost doctor</code> in your terminal to verify.</li>
         </ol>`
      : `<p>Nollpost encountered an error while reading Mail.app mailboxes. Make sure Mail.app is running and try again, or run <code>nollpost doctor</code> in your terminal.</p>`;
    return `
      <div class="empty-state scan-error">
        <h3>${title}</h3>
        ${stepsHtml}
        <pre style="white-space:pre-wrap;word-break:break-word;max-width:100%;font-size:11px;background:var(--bg-primary);padding:8px 10px;border-radius:8px;border:1px solid var(--border);">${escapeHtml(rawMessage)}</pre>
        <div class="empty-state-actions">
          <button class="btn small primary" data-retry-scan="1">Retry Scan</button>
        </div>
      </div>`;
  }

  function showError(msg) {
    const resEl = qs("results-banner");
    resEl.dataset.kind = "error";
    resEl.classList.remove("hidden");
    const tip = isMailPermissionError(msg)
      ? `<div style="margin-top:4px;font-size:11px;color:var(--text-secondary);">Permission tip: Check <strong>System Settings → Privacy &amp; Security → Automation</strong> (enable Mail) and run <code>nollpost doctor</code>.</div>`
      : "";
    resEl.innerHTML = `<div class="result-row"><div><span class="pill danger">error</span> ${escapeHtml(msg)}${tip}</div></div>`;
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
    qs("board-title").textContent =
      state.mode === "catchup"
        ? `Nollpost Archive Catch-Up${acctSuffix}`
        : `Nollpost${acctSuffix}`;

    const st = d.stats || {};
    qs("tally-pills").innerHTML = `
      <span class="pill green">Lifetime: ${st.total_unsubscribed || 0} unsubbed (${st.unique_senders_unsubscribed || 0} senders)</span>
      <span class="pill accent">${st.total_archived || 0} archived</span>
      <span class="pill accent">${st.total_declined || 0} declined</span>
      <span class="pill muted">0 deleted</span>
      <span class="pill warn">Missed-unsub queue: ${st.missed_unsub_pending || 0}</span>
    `;

    qs("mode-inbox").classList.toggle("active", state.mode === "inbox");
    qs("mode-catchup").classList.toggle("active", state.mode === "catchup");

    const allItems = d.items || [];
    const visItems = filteredItems();
    const selItems = allItems.filter((i) => state.selectedUids.has(i.uid));

    const allPairs = nonNoopPairs(allItems);
    const catPairs = nonNoopPairs(visItems);
    const selPairs = nonNoopPairs(selItems);

    const catLabel = CATEGORIES.find((c) => c.id === state.category)?.label || "Current";
    qs("btn-reset-actions").disabled = catPairs.length === 0 || state.category === "missed";
    qs("btn-exec-selected").textContent = `Execute Selected (${selPairs.length})`;
    qs("btn-exec-selected").disabled = selPairs.length === 0;
    qs("btn-exec-category").textContent = `Execute ${catLabel} (${catPairs.length})`;
    qs("btn-exec-category").disabled = catPairs.length === 0 || state.category === "missed";
    qs("btn-exec-all").textContent = `Execute All Actions (${allPairs.length})`;
    qs("btn-exec-all").disabled = allPairs.length === 0;

    const counts = {};
    for (const p of allPairs) {
      counts[p.action] = (counts[p.action] || 0) + 1;
    }
    const summaryBits = Object.entries(counts).map(([k, v]) => `${v} ${ACTION_LABELS[k] || k}`);
    const warnTxt = (d.warnings || []).length ? ` | Warnings: ${d.warnings.join("; ")}` : "";
    qs("batch-summary").textContent =
      (summaryBits.length ? `Queued defaults: ${summaryBits.join(" · ")}` : "No pending actions selected") + warnTxt;

    const tray = d.tray || [];
    const trayEl = qs("safety-tray");
    if (tray.length > 0) {
      trayEl.classList.remove("hidden");
      const names = tray.slice(0, 4).map((t) => escapeHtml(t.subject.slice(0, 36))).join(", ");
      const more = tray.length > 4 ? ` +${tray.length - 4} more` : "";
      qs("tray-text").innerHTML = `<strong>Step 2 Safety Tray:</strong> ${tray.length} opened item(s) waiting in Inbox (${names}${more})`;
      qs("btn-tray-sweep").textContent = `Review & Sweep ${tray.length} Opened…`;
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
    const visibleCats = CATEGORIES.filter(
      (c) => c.id !== "consulting" || d.consulting_enabled || catCounts.consulting > 0
    );
    if (!visibleCats.some((c) => c.id === state.category)) {
      state.category = "all";
    }
    qs("category-tabs").innerHTML = visibleCats.map(
      (c) =>
        `<button class="tab ${state.category === c.id ? "active" : ""}" data-cat="${c.id}">${escapeHtml(c.label)} (${catCounts[c.id] || 0})</button>`
    ).join("");

    const acctList = d.accounts || [];
    if (state.account !== "all" && !acctList.some((a) => a.name === state.account)) {
      state.account = "all";
    }
    if (acctList.length <= 1) {
      qs("account-tabs").innerHTML = "";
    } else {
      const accts = [{ id: "all", label: `All Accounts (${allItems.length})` }];
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
      qs("queue-container").innerHTML = `<div style="padding:20px;text-align:center;color:var(--text-secondary);">No unresolved missed-unsub items queued.</div>`;
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
              <span>Msg ID #${escapeHtml(m.internal_id)}</span>
              <span>· ${escapeHtml(m.failure_reason)}</span>
            </div>
          </td>
          <td>
            <div class="seg">
              <button data-missed-inspect="${m.id}">Inspect Snapshot</button>
              ${firstLink ? `<button data-missed-open="${escapeHtml(firstLink)}">Open Link</button>` : ""}
              ${firstLink ? `<button data-missed-retry="${m.id}">Retry Playwright</button>` : ""}
              <button data-missed-mark-unsub="${m.id}">Mark Unsubscribed &amp; Archive</button>
              <button data-missed-resolve="${m.id}">Dismiss</button>
            </div>
          </td>
        </tr>`;
      })
      .join("");

    qs("queue-container").innerHTML = `
      <table class="tbl">
        <thead>
          <tr><th>Acct</th><th>Stage</th><th>Sender, Subject &amp; Failure Reason</th><th>Actions</th></tr>
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
            <h3>${isCatchup ? "Archive Catch-Up Complete" : "Inbox Zero!"}</h3>
            <p>${isCatchup
              ? "No repeat newsletters needing unsubscription were found in your Archive folders."
              : "All scanned inboxes are empty. Nothing queued or pending right now."}</p>
            <div class="empty-state-actions">
              <button class="btn small primary" data-retry-scan="1">Refresh Scan</button>
              ${!isCatchup
                ? `<button class="btn small secondary" data-mode="catchup">Check Archive Catch-Up</button>`
                : `<button class="btn small secondary" data-mode="inbox">Back to Daily Inbox</button>`}
            </div>
          </div>`;
        return;
      }
      qs("queue-container").innerHTML = `
        <div class="empty-state">
          <h3>No emails match the current filters</h3>
          <p>Adjust your search query, account tab, category tab, or Unread filter to see queued items.</p>
          <div class="empty-state-actions">
            <button class="btn small primary" id="btn-reset-filters">Reset filters</button>
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
              title = "Disabled: no reliable unsubscribe target found";
            } else if (act === "read_open_link" && item.extracted?.pertinent_url) {
              title = item.extracted.pertinent_url;
            }
            return `<button class="${active ? "active" : ""}" ${!allowed ? "disabled" : ""} title="${escapeHtml(title)}" data-uid="${escapeHtml(item.uid)}" data-act="${act}">${escapeHtml(ACTION_LABELS[act] || act)}</button>`;
          })
          .join("");

        const queueMissedBtn =
          item.no_unsub_found && state.mode === "inbox"
            ? `<button class="btn small" data-enqueue-missed="${escapeHtml(item.uid)}">+ Queue Missed Unsub</button>`
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
            <strong>${escapeHtml(h.subject || "(no subject)")}</strong>
            <div class="sub-meta">
              <span>${escapeHtml(h.sender)}</span>
              <span>· ${escapeHtml((h.date_received || "").slice(0, 16).replace("T", " "))}</span>
              ${badgesHtml}
              <select class="pref-select" data-pref-email="${escapeHtml(h.sender_email)}" data-pref-dom="${escapeHtml(h.sender_domain)}" data-pref-name="${escapeHtml(h.sender)}">
                <option value="">Remember…</option>
                <option value="always_keep">Always Keep / Updates</option>
                <option value="archive">Always Archive</option>
                <option value="mark_read">Always Mark Read</option>
                <option value="clear">Clear Learned Rule</option>
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
            <th>Acct</th>
            <th>Category</th>
            <th>Subject, Sender &amp; Signals</th>
            <th>Per-Email Action Toggle</th>
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
    qs("queue-container").innerHTML = `<div style="padding:20px;color:var(--text-secondary);" id="scan-status-text">Scanning Mail.app (${escapeHtml(state.mode)})…</div>`;
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
      .map(([act, cnt]) => `<div class="confirm-stat"><strong>${cnt}</strong>${escapeHtml(ACTION_LABELS[act] || act)}</div>`)
      .join("");

    let detailsHtml = "";
    if ((preview.send_declines || []).length > 0) {
      detailsHtml += `<div class="confirm-warning danger"><strong>Outbound Email Warning:</strong> This run will automatically send <strong>${preview.send_declines.length}</strong> real email reply(s) from Mail.app. Double-check the recipients below before confirming.</div>`;
    }
    if ((preview.unsubs || []).length > 10) {
      detailsHtml += `<div class="confirm-warning warn"><strong>Large Unsubscribe Batch:</strong> You are about to unsubscribe from <strong>${preview.unsubs.length}</strong> senders at once. Review the list below to make sure no wanted senders are included.</div>`;
    }
    if (archSummary) {
      detailsHtml += `<div class="preview-box"><strong>Archive Destinations (Mark Read + Move):</strong> ${escapeHtml(archSummary)}</div>`;
    }
    if ((preview.links_to_open || []).length) {
      detailsHtml += `<div class="preview-box"><strong>Open in Browser + Safety Tray:</strong><br/>${preview.links_to_open
        .map((l) => `• ${escapeHtml(l.subject)} → <code>${escapeHtml(l.url)}</code>`)
        .join("<br/>")}</div>`;
    }
    if ((preview.pdfs_to_open || []).length) {
      detailsHtml += `<div class="preview-box"><strong>Open Invoices in Preview + Safety Tray:</strong><br/>${preview.pdfs_to_open
        .map((p) => `• ${escapeHtml(p.subject)} (${escapeHtml(p.filename)})`)
        .join("<br/>")}</div>`;
    }
    if ((preview.unsubs || []).length) {
      detailsHtml += `<div class="preview-box"><strong>Unsubscribe Senders (${preview.unsubs.length}):</strong><br/>${preview.unsubs
        .map((u) => `• ${escapeHtml(u.sender)} [${escapeHtml(u.method)}] → <code>${escapeHtml(u.url)}</code>`)
        .join("<br/>")}</div>`;
    }
    if ((preview.declines || []).length) {
      detailsHtml += `<div class="preview-box"><strong>Consulting Declines (Playwright button):</strong><br/>${preview.declines
        .map((d) => `• ${escapeHtml(d.sender)} · ${escapeHtml(d.subject)} → <code>${escapeHtml(d.url)}</code>`)
        .join("<br/>")}</div>`;
    }
    if ((preview.draft_declines || []).length) {
      detailsHtml += `<div class="preview-box"><strong>Boilerplate Drafts in Mail.app (kept in Inbox):</strong><br/>${preview.draft_declines
        .map((d) => `• ${escapeHtml(d.sender)} · ${escapeHtml(d.subject)}`)
        .join("<br/>")}</div>`;
    }
    if ((preview.send_declines || []).length) {
      detailsHtml += `<div class="preview-box"><strong>Auto-Send Boilerplate Replies &amp; Archive:</strong><br/>${preview.send_declines
        .map((d) => `• ${escapeHtml(d.sender)} · ${escapeHtml(d.subject)}`)
        .join("<br/>")}<pre style="margin-top:6px;">${escapeHtml(preview.boilerplate_preview)}</pre></div>`;
    }

    qs("modal-title").textContent = `${title} (${preview.total_actions} actions)`;
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
    progEl.textContent = `Running 0/${pairs.length} actions…`;

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
        progEl.textContent = `Running ${job.done}/${job.total}: ${job.message}`;
        if (job.status === "completed") {
          progEl.classList.add("hidden");
          renderJobResults(job.results || [], job.scan_warning || "");
          await loadState(false);
          break;
        }
        if (job.status === "error") {
          progEl.textContent = `Execution error: ${job.message}`;
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
      ? `<div class="result-row"><div><span class="pill warn">warning</span> ${escapeHtml(scanWarning)}</div></div>`
      : "";
    const rows = results
      .map((r, idx) => {
        const cls = r.status === "ok" || r.status === "drafted" ? "green" : r.status === "blocked_captcha" ? "warn" : "danger";
        const btns = [];
        if (r.status === "blocked_captcha" && isSafeHttpUrl(r.opened_target)) {
          btns.push(`<button class="btn small" data-open-url="${escapeHtml(r.opened_target)}">Open in Browser</button>`);
        }
        if (r.screenshot_path) {
          btns.push(`<button class="btn small" data-proof-idx="${idx}">View Screenshot</button>`);
        } else if (isSafeHttpUrl(r.opened_target) && ["archive_and_unsub", "unsub_only", "decline_and_archive"].includes(r.action)) {
          btns.push(`<button class="btn small" data-proof-idx="${idx}">View Proof</button>`);
        }
        if (r.status !== "ok" && ["archive_and_unsub", "unsub_only"].includes(r.action)) {
          const lbl = r.action === "unsub_only" ? "Mark Unsubscribed" : "Mark Unsubscribed & Archive";
          btns.push(`<button class="btn small" data-mark-unsub-idx="${idx}">${escapeHtml(lbl)}</button>`);
        }
        return `<div class="result-row">
          <div><span class="pill ${cls}">${escapeHtml(r.status)}</span> <strong>${escapeHtml(r.sender)}</strong> · ${escapeHtml(r.detail)}</div>
          <div style="display:flex;gap:6px;flex-wrap:wrap;">${btns.join("")}</div>
        </div>`;
      })
      .join("");
    resEl.innerHTML = `<div style="display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:6px;margin-bottom:6px;"><strong>Last Run Results (${results.length})</strong><span style="font-size:11px;color:var(--text-secondary);">Note: Results and screenshots reset when you start the next run.</span></div>${warnRow}${rows}`;
  }

  function showProofModal(idx) {
    const r = state.lastJobResults[idx];
    if (!r) return;
    qs("snap-title").textContent = `Execution Proof: ${r.sender}`;
    const shotName = r.screenshot_path ? r.screenshot_path.split("/").pop() : "";
    const shotHtml = shotName
      ? `<div class="preview-box"><strong>Confirmation Screenshot:</strong><br/><img src="/api/screenshots/${encodeURIComponent(shotName)}" style="max-width:100%;border-radius:6px;margin-top:6px;" /></div>`
      : `<div class="preview-box" style="color:var(--text-secondary);">Completed via Tier 1 HTTP POST (no browser screenshot captured).</div>`;
    const urlHtml = isSafeHttpUrl(r.opened_target)
      ? `<div class="preview-box"><strong>Endpoint URL:</strong><br/><code>${escapeHtml(r.opened_target)}</code></div>`
      : "";
    const markBtnHtml =
      r.status !== "ok" && ["archive_and_unsub", "unsub_only"].includes(r.action)
        ? `<div style="margin-top:8px;"><button class="btn small primary" data-mark-unsub-idx="${idx}">${escapeHtml(
            r.action === "unsub_only" ? "Mark as Unsubscribed" : "Mark as Unsubscribed & Archive"
          )}</button></div>`
        : "";
    qs("snap-body").innerHTML = `
      <div class="preview-box"><strong>Status:</strong> ${escapeHtml(r.status)}<br/><strong>Action:</strong> ${escapeHtml(ACTION_LABELS[r.action] || r.action)}<br/><strong>Detail:</strong> ${escapeHtml(r.detail)}${markBtnHtml}</div>
      ${urlHtml}
      ${shotHtml}
    `;
    qs("snapshot-modal").classList.remove("hidden");
  }

  async function openTrayConfirmation() {
    const tray = state.data?.tray || [];
    if (!tray.length) return;
    const uids = tray.map((t) => t.message_uid);
    const prev = await api("/api/tray/sweep/preview", {
      method: "POST",
      body: JSON.stringify({ uids }),
    });
    const destStr = Object.entries(prev.archive_targets || {})
      .map(([a, b]) => Object.entries(b).map(([m, c]) => `${a} → ${m} (${c})`).join(", "))
      .join(" | ");
    qs("modal-title").textContent = `Confirm Step 2 Sweep (${prev.count} opened items)`;
    const itemRows = (prev.items || [])
      .map((i) => {
        const bestUrl = isSafeHttpUrl(i.latest_pertinent_url)
          ? i.latest_pertinent_url
          : isSafeHttpUrl(i.opened_target)
          ? i.opened_target
          : "";
        const changedTag =
          i.latest_pertinent_url && i.opened_target && i.latest_pertinent_url !== i.opened_target
            ? ` <span class="pill accent">Updated link</span>`
            : "";
        const reopenBtn = bestUrl
          ? ` <button type="button" class="btn small" data-open-url="${escapeHtml(bestUrl)}">Re-open Link</button>`
          : "";
        const dismissItemBtn = `<button type="button" class="btn small secondary" data-tray-dismiss-uid="${escapeHtml(i.uid)}">Dismiss</button>`;
        const urlLine = bestUrl ? `<div style="margin-top:2px;font-size:11px;color:var(--text-secondary);word-break:break-all;"><code>${escapeHtml(bestUrl)}</code></div>` : "";
        return `<div style="padding:6px 0;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;gap:8px;">
          <div>• [${escapeHtml(i.account)}] <strong>${escapeHtml(i.subject)}</strong> · ${escapeHtml(i.sender)}${changedTag}${urlLine}</div>
          <div style="flex-shrink:0;display:flex;gap:6px;">${reopenBtn}${dismissItemBtn}</div>
        </div>`;
      })
      .join("");
    qs("modal-body").innerHTML = `
      <div class="preview-box"><strong>Archive Destinations:</strong> ${escapeHtml(destStr)}</div>
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
    qs("snap-title").textContent = `Missed Unsubscribe #${entry.id}: ${entry.sender}`;
    const links = (entry.candidate_links || [])
      .filter((l) => isSafeHttpUrl(l.href))
      .map((l) => `• ${escapeHtml(l.text || "(no text)")} → <a href="${escapeHtml(l.href)}" target="_blank" rel="noopener noreferrer">${escapeHtml(l.href)}</a>`)
      .join("<br/>");
    const shotName = entry.screenshot_path ? entry.screenshot_path.split("/").pop() : "";
    const shotHtml = shotName
      ? `<div class="preview-box"><strong>Screenshot:</strong><br/><img src="/api/screenshots/${encodeURIComponent(shotName)}" style="max-width:100%;border-radius:6px;margin-top:6px;" /></div>`
      : "";
    const finalUrlHtml = isSafeHttpUrl(entry.final_url)
      ? `<br/><strong>Target URL:</strong> <a href="${escapeHtml(entry.final_url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(entry.final_url)}</a>`
      : "";
    qs("snap-body").innerHTML = `
      <div class="preview-box"><strong>Reason:</strong> ${escapeHtml(entry.failure_reason)}<br/><strong>Mail ID:</strong> ${escapeHtml(entry.account)}/${escapeHtml(entry.mailbox)} #${escapeHtml(entry.internal_id)}${finalUrlHtml}<div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap;align-items:center;"><button class="btn small primary" data-missed-mark-unsub="${entry.id}">Mark as Unsubscribed &amp; Archive</button><button class="btn small secondary" data-missed-copy-report="${entry.id}">Copy Redacted Bug Report</button><span id="missed-copy-status" style="font-size:11px;color:var(--text-secondary);"></span></div></div>
      <div id="missed-redacted-preview" class="preview-box hidden"></div>
      ${shotHtml}
      <div class="preview-box"><strong>Candidate Links:</strong><br/>${links || "(none)"}</div>
      <div class="preview-box"><strong>Headers:</strong><pre>${escapeHtml(entry.headers_snapshot)}</pre></div>
      <div class="preview-box"><strong>Footer HTML Snippet:</strong><pre>${escapeHtml(entry.footer_html_snapshot)}</pre></div>
    `;
    qs("snapshot-modal").classList.remove("hidden");
  }

  function showQuickGuideModal() {
    qs("snap-title").textContent = "Nollpost Quick Guide";
    qs("snap-body").innerHTML = `
      <div class="preview-box">
        <strong>Archive-Only Guarantee</strong>
        <p style="margin-top:4px;line-height:1.45;">Nollpost never deletes emails. Archiving marks a message read and moves it to that account's configured Archive mailbox.</p>
      </div>
      <div class="preview-box">
        <strong>Key Concepts</strong>
        <ul class="guide-list" style="margin-top:6px;">
          <li><strong>Step 2 Safety Tray:</strong> Items opened via <em>Read + Open link</em> or <em>Read + Show invoice</em> stay in your Mail.app Inbox and appear in the Safety Tray so you can finish reading before clicking <em>Review &amp; Sweep Opened…</em> to archive them.</li>
          <li><strong>Archive Catch-Up:</strong> Scans your Archive folders for repeat promotional newsletters and offers <em>Unsub only</em> without modifying Inbox state.</li>
          <li><strong>Missed-Unsub Queue:</strong> Stores senders where automatic unsubscribe lacked a reliable link or failed in headless mode. Inspect snapshots, retry, or mark resolved once handled manually.</li>
          <li><strong>Show Browser:</strong> Leave unchecked for background headless Playwright runs; check it to watch browser interactions live.</li>
          <li><strong>Remember… dropdown:</strong> Teach Nollpost per sender (<em>Always Keep</em>, <em>Always Archive</em>, or <em>Always Mark Read</em>).</li>
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

    qs("snap-title").textContent = "Nollpost Settings";
    qs("snap-body").innerHTML = `
      <div style="font-size:11px;color:var(--text-secondary);">Config file: <code>${escapeHtml(cfg.config_path || "")}</code></div>
      <div class="preview-box" style="max-height:none;">
        <strong>Scan &amp; Archiving</strong>
        <div class="settings-grid" style="margin-top:8px;">
          <div class="settings-field">
            <label for="cfg-max-inbox">Max Inbox Messages (1–5000)</label>
            <input id="cfg-max-inbox" type="number" min="1" max="5000" value="${Number(u.max_inbox_messages || 500)}" />
          </div>
          <div class="settings-field">
            <label for="cfg-catchup-limit">Catch-Up Scan Limit (1–5000)</label>
            <input id="cfg-catchup-limit" type="number" min="1" max="5000" value="${Number(u.catchup_scan_limit || 200)}" />
          </div>
          <div class="settings-field">
            <label for="cfg-catchup-days">Catch-Up Days Back (1–365)</label>
            <input id="cfg-catchup-days" type="number" min="1" max="365" value="${Number(u.catchup_days_back || 14)}" />
          </div>
          <div class="settings-field">
            <label for="cfg-gmail-strategy">Gmail Archive Strategy</label>
            <select id="cfg-gmail-strategy">${stratOpts}</select>
          </div>
        </div>
        <div style="margin-top:8px;">
          <label class="toggle-check">
            <input type="checkbox" id="cfg-mark-read" ${u.mark_read_on_archive !== false ? "checked" : ""} />
            <span>Mark messages as read when archiving</span>
          </label>
        </div>
        <div class="settings-field" style="margin-top:8px;">
          <label for="cfg-archive-map">Per-Account Archive Overrides (one <code>Account = Mailbox</code> per line; Trash/Junk blocked)</label>
          <textarea id="cfg-archive-map" placeholder="${escapeHtml(acctPlaceholder)}">${escapeHtml(archLines)}</textarea>
        </div>
      </div>

      <div class="preview-box" style="max-height:none;">
        <strong>Domains, Keywords &amp; Rule Packs</strong>
        <div style="margin-top:6px;display:flex;flex-direction:column;gap:4px;">${packChecks || "<em>No built-in packs found</em>"}</div>
        <div class="settings-grid" style="margin-top:8px;">
          <div class="settings-field">
            <label for="cfg-wanted-domains">Wanted Domains (comma or newline separated)</label>
            <textarea id="cfg-wanted-domains">${escapeHtml((p.wanted_domains || []).join(", "))}</textarea>
          </div>
          <div class="settings-field">
            <label for="cfg-bill-keywords">Bill &amp; Finance Keywords</label>
            <textarea id="cfg-bill-keywords">${escapeHtml((p.bill_keywords || []).join(", "))}</textarea>
          </div>
          <div class="settings-field">
            <label for="cfg-transactional-domains">Transactional / Delivery Domains</label>
            <textarea id="cfg-transactional-domains">${escapeHtml((p.transactional_domains || []).join(", "))}</textarea>
          </div>
          <div class="settings-field">
            <label for="cfg-browser-locale">Browser Locale Override (blank = ${escapeHtml(b.effective_locale || "en-US")})</label>
            <input id="cfg-browser-locale" type="text" value="${escapeHtml(b.locale || "")}" placeholder="e.g. sv-SE or en-US" />
            <label for="cfg-accept-lang" style="margin-top:4px;">Accept-Language Override</label>
            <input id="cfg-accept-lang" type="text" value="${escapeHtml(b.accept_language || "")}" placeholder="${escapeHtml(b.effective_accept_language || "en-US,en;q=0.9")}" />
          </div>
        </div>
      </div>

      <div class="preview-box" style="max-height:none;">
        <strong>Consulting Outreach</strong>
        <div style="margin-top:6px;">
          <label class="toggle-check">
            <input type="checkbox" id="cfg-consult-enabled" ${c.enabled ? "checked" : ""} />
            <span>Enable Consulting Outreach classification</span>
          </label>
        </div>
        <div class="settings-grid" style="margin-top:8px;">
          <div class="settings-field">
            <label for="cfg-consult-networks">Consulting Networks</label>
            <textarea id="cfg-consult-networks">${escapeHtml((c.networks || []).join(", "))}</textarea>
          </div>
          <div class="settings-field">
            <label for="cfg-consult-match">Match Keywords</label>
            <textarea id="cfg-consult-match">${escapeHtml((c.match_keywords || []).join(", "))}</textarea>
          </div>
          <div class="settings-field">
            <label for="cfg-consult-negative">Negative Keywords</label>
            <textarea id="cfg-consult-negative">${escapeHtml((c.negative_keywords || []).join(", "))}</textarea>
          </div>
          <div class="settings-field">
            <label for="cfg-consult-boilerplate">Decline Boilerplate</label>
            <textarea id="cfg-consult-boilerplate">${escapeHtml(c.decline_boilerplate || "")}</textarea>
          </div>
        </div>
      </div>

      <div class="settings-footer">
        <span id="settings-status" class="modal-status">Changes save to config.toml and apply on the next scan without restarting.</span>
        <button class="btn small primary" id="btn-save-settings">Save Settings &amp; Rescan</button>
      </div>
    `;
    qs("snapshot-modal").classList.remove("hidden");
  }

  function formatSenderRuleStatus(row) {
    const tags = [];
    if (row.preferred_action === "always_keep") tags.push(`<span class="pill green">Always Keep</span>`);
    else if (row.preferred_action === "archive") tags.push(`<span class="pill accent">Always Archive</span>`);
    else if (row.preferred_action === "mark_read") tags.push(`<span class="pill muted">Always Mark Read</span>`);
    if (row.unsubscribed_at || row.unsub_count > 0) {
      tags.push(`<span class="pill warn">Unsubscribed ×${Number(row.unsub_count || 0)}</span>`);
    }
    if (!tags.length) tags.push(`<span class="pill muted">Seen</span>`);
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
                  <option value="">Change…</option>
                  <option value="always_keep">Always Keep</option>
                  <option value="archive">Always Archive</option>
                  <option value="mark_read">Always Mark Read</option>
                  <option value="clear">Clear Learned Rule</option>
                  <option value="delete">Delete Entry</option>
                </select>
              </td>
            </tr>`;
          })
          .join("")
      : `<tr><td colspan="5" style="text-align:center;color:var(--text-secondary);padding:16px;">No sender memory records match the current filter.</td></tr>`;
  }

  function renderSenderRulesRowsOnly() {
    const items = state.rulesItems || [];
    const allSel = items.length > 0 && items.every((r) => state.rulesSelectedKeys.has(r.sender_key));
    qs("snap-title").textContent = `Sender Rules & Memory (${items.length})`;
    const tbody = qs("rules-tbody");
    if (tbody) tbody.innerHTML = renderSenderRulesRowsHtml(items);
    const mcb = qs("rules-master-cb");
    if (mcb instanceof HTMLInputElement) mcb.checked = allSel;
    const bulkBtn = qs("btn-rules-bulk-apply");
    if (bulkBtn instanceof HTMLButtonElement) bulkBtn.disabled = state.rulesSelectedKeys.size === 0;
    const bulkSel = qs("rules-bulk-action");
    if (bulkSel instanceof HTMLSelectElement && bulkSel.options.length > 0) {
      bulkSel.options[0].textContent = `Bulk action on ${state.rulesSelectedKeys.size} selected…`;
    }
  }

  function renderSenderRulesModalBody() {
    const kinds = [
      { id: "all", label: "All" },
      { id: "always_keep", label: "Always Keep" },
      { id: "archive", label: "Always Archive" },
      { id: "mark_read", label: "Always Mark Read" },
      { id: "unsubscribed", label: "Unsubscribed" },
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

    qs("snap-title").textContent = `Sender Rules & Memory (${items.length})`;
    qs("snap-body").innerHTML = `
      <div class="rules-toolbar">
        <div class="tabs">${filterHtml}</div>
        <input type="search" id="rules-search-input" class="search-box" value="${escapeHtml(state.rulesSearch)}" placeholder="Search sender or display name…" />
      </div>
      <div class="rules-toolbar">
        <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;">
          <select id="rules-bulk-action" class="pref-select" style="font-size:12px;padding:4px 8px;">
            <option value="">Bulk action on ${state.rulesSelectedKeys.size} selected…</option>
            <option value="always_keep">Set Always Keep</option>
            <option value="archive">Set Always Archive</option>
            <option value="mark_read">Set Always Mark Read</option>
            <option value="clear">Clear Learned Rules</option>
            <option value="delete">Delete Entries Completely</option>
          </select>
          <button class="btn small secondary" id="btn-rules-bulk-apply" ${state.rulesSelectedKeys.size === 0 ? "disabled" : ""}>Apply Bulk Action</button>
        </div>
        <span id="rules-status" class="modal-status"></span>
      </div>
      <div class="tbl-wrap">
        <table class="tbl">
          <thead>
            <tr>
              <th><input type="checkbox" id="rules-master-cb" ${allSel ? "checked" : ""} /></th>
              <th>Sender</th>
              <th>Rule / History</th>
              <th>Last Seen</th>
              <th>Edit</th>
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
              ? ` <span class="pill accent">Restored ${escapeHtml(String(row.undone_at).slice(0, 16).replace("T", " "))}</span>`
              : "";
            const undoBtn = row.can_undo_archive
              ? `<button class="btn small secondary" data-audit-undo="${row.id}">Undo Archive</button>`
              : "";
            const actLabel =
              ACTION_LABELS[row.action] ||
              { sweep_archive: "Tray Sweep", undo_archive: "Undo Archive", mark_unsubscribed: "Mark Unsub" }[row.action] ||
              row.action;
            return `<tr>
              <td style="font-size:11px;color:var(--text-secondary);white-space:nowrap;">${escapeHtml((row.created_at || "").slice(0, 16).replace("T", " "))}</td>
              <td><span class="acct ${accountToneClass(row.account)}">${escapeHtml(row.account)}</span></td>
              <td><span class="pill ${stCls}">${escapeHtml(st)}</span> <span class="pill muted">${escapeHtml(actLabel)}</span></td>
              <td>
                <strong>${escapeHtml(row.subject || "(no subject)")}</strong>
                <div class="sub-meta">
                  <span>${escapeHtml(row.sender)}</span>
                  <span>· Run <code>${escapeHtml((row.run_id || "").slice(0, 14))}</code></span>
                  <span>· ${escapeHtml(row.detail)}</span>
                  ${undoneTag}
                </div>
              </td>
              <td>${undoBtn}</td>
            </tr>`;
          })
          .join("")
      : `<tr><td colspan="5" style="text-align:center;color:var(--text-secondary);padding:16px;">No audit log entries match the current filters.</td></tr>`;
  }

  function renderAuditLogRowsOnly() {
    const items = state.auditItems || [];
    qs("snap-title").textContent = `Audit Log & Batch History (${items.length})`;
    const tbody = qs("audit-tbody");
    if (tbody) tbody.innerHTML = renderAuditLogRowsHtml(items);
  }

  function renderAuditLogModalBody() {
    const statuses = [
      { id: "all", label: "All" },
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
      `<option value="">All Batches (${(state.auditRuns || []).length})</option>`,
      ...(state.auditRuns || []).map((r) => {
        const ts = String(r.created_at || "").slice(0, 16).replace("T", " ");
        const lbl = `${r.run_id} (${ts} · ${r.total} items: ${r.ok} ok, ${r.failed} failed)`;
        const sel = state.auditRunId === r.run_id ? "selected" : "";
        return `<option value="${escapeHtml(r.run_id)}" ${sel}>${escapeHtml(lbl)}</option>`;
      }),
    ].join("");
    const items = state.auditItems || [];
    qs("snap-title").textContent = `Audit Log & Batch History (${items.length})`;
    qs("snap-body").innerHTML = `
      <div class="rules-toolbar">
        <div class="tabs">${statusTabs}</div>
        <input type="search" id="audit-search-input" class="search-box" value="${escapeHtml(state.auditSearch)}" placeholder="Search sender, subject, or detail…" />
      </div>
      <div class="rules-toolbar">
        <select id="audit-run-select" class="pref-select" style="font-size:12px;padding:4px 8px;">${runOpts}</select>
        <span id="audit-status" class="modal-status">Undo Archive safely restores archived emails back to INBOX (0 deletions).</span>
      </div>
      <div class="tbl-wrap">
        <table class="tbl">
          <thead>
            <tr>
              <th>Time</th>
              <th>Acct</th>
              <th>Outcome &amp; Action</th>
              <th>Message &amp; Detail</th>
              <th>Restore</th>
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
    const t = ev.target;
    if (!(t instanceof HTMLElement)) return;
    try {
      if (t.id === "btn-save-settings") {
        const statusEl = qs("settings-status");
        const saveBtn = t instanceof HTMLButtonElement ? t : null;
        if (saveBtn) saveBtn.disabled = true;
        if (statusEl) {
          statusEl.className = "modal-status";
          statusEl.textContent = "Saving settings…";
        }
        try {
          const packs = [...document.querySelectorAll(".cfg-pack-cb:checked")].map((el) => el.value);
          const payload = {
            user: {
              mark_read_on_archive: Boolean(qs("cfg-mark-read")?.checked),
              max_inbox_messages: Number(qs("cfg-max-inbox")?.value || 500),
              catchup_scan_limit: Number(qs("cfg-catchup-limit")?.value || 200),
              catchup_days_back: Number(qs("cfg-catchup-days")?.value || 14),
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
          state.actionsByUid.clear();
          qs("snapshot-modal").classList.add("hidden");
          await loadState(true);
        } catch (saveErr) {
          if (statusEl) {
            statusEl.className = "modal-status err";
            statusEl.textContent = `Save failed: ${saveErr.message}`;
          }
        } finally {
          if (saveBtn) saveBtn.disabled = false;
        }
        return;
      }
      if (t.dataset.rulesKind) {
        state.rulesFilter = t.dataset.rulesKind;
        await refreshSenderRulesData();
        return;
      }
      if (t.dataset.auditStatus) {
        state.auditStatus = t.dataset.auditStatus;
        await refreshAuditLogData();
        return;
      }
      if (t.dataset.auditUndo) {
        const undoTriggerBtn = t instanceof HTMLButtonElement ? t : null;
        const entryId = Number(t.dataset.auditUndo);
        const row = (state.auditItems || []).find((r) => r.id === entryId);
        qs("modal-title").textContent = "Confirm Undo Archive";
        qs("modal-body").innerHTML = row
          ? `<div class="confirm-warning warn">Move <strong>${escapeHtml(row.subject || "(no subject)")}</strong> from <em>${escapeHtml(row.sender)}</em> back to <strong>${escapeHtml(row.account)}/INBOX</strong>? (Archive-only safeguard preserved; 0 emails deleted.)</div>`
          : `<div class="confirm-warning warn">Restore archived message back to <strong>INBOX</strong>?</div>`;
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
              statusEl.textContent = `Undo failed: ${undoErr.message}`;
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
            nextEl.textContent = `Restored to ${resp.restored_to || "INBOX"}.`;
          }
          try {
            await loadState(false);
          } catch (scanErr) {
            showError(scanErr.message);
          }
        };
        return;
      }
      if (t.id === "btn-rules-bulk-apply") {
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
            nextStatus.textContent = `Updated ${keys.length} sender(s).`;
          }
        };
        if (act === "delete") {
          qs("modal-title").textContent = `Delete ${keys.length} Sender Memory Entries`;
          qs("modal-body").innerHTML = `<div class="confirm-warning warn">Remove <strong>${keys.length}</strong> sender record(s) from SQLite <code>sender_memory</code>? Learned rules and unsubscribe cooldown history for these senders will be reset.</div>`;
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
      if (t.id === "btn-reset-filters") {
        state.category = "all";
        state.account = "all";
        state.unreadOnly = false;
        state.search = "";
        qs("unread-only-toggle").checked = false;
        qs("search-input").value = "";
        render();
        return;
      }
      if (t.dataset.retryScan) {
        await loadState(true);
        return;
      }
      if (t.dataset.trayDismissUid) {
        const uid = t.dataset.trayDismissUid;
        if (t instanceof HTMLButtonElement) t.disabled = true;
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
          if (t instanceof HTMLButtonElement) t.disabled = false;
          qs("modal-confirm").disabled = false;
        }
        return;
      }
      if (t.dataset.openUrl && isSafeHttpUrl(t.dataset.openUrl)) {
        window.open(t.dataset.openUrl, "_blank", "noopener,noreferrer");
        return;
      }
      if (t.dataset.cat) {
        state.category = t.dataset.cat;
        render();
        return;
      }
      if (t.dataset.acct) {
        state.account = t.dataset.acct;
        render();
        return;
      }
      if (t.dataset.mode) {
        state.mode = t.dataset.mode;
        state.category = "all";
        await loadState(false);
        return;
      }
      if (t.dataset.uid && t.dataset.act) {
        state.actionsByUid.set(t.dataset.uid, t.dataset.act);
        render();
        return;
      }
      if (t.dataset.enqueueMissed) {
        await api("/api/missed/enqueue", {
          method: "POST",
          body: JSON.stringify({ uid: t.dataset.enqueueMissed }),
        });
        await loadState(false);
        return;
      }
      if (t.dataset.proofIdx !== undefined) {
        showProofModal(Number(t.dataset.proofIdx));
        return;
      }
      if (t.dataset.missedInspect) {
        showMissedSnapshot(Number(t.dataset.missedInspect));
        return;
      }
      if (t.dataset.missedCopyReport) {
        const mid = Number(t.dataset.missedCopyReport);
        const statusEl = qs("missed-copy-status");
        const prevEl = qs("missed-redacted-preview");
        let rep;
        try {
          rep = await api(`/api/missed/${mid}/export`);
        } catch (copyErr) {
          if (statusEl) statusEl.textContent = `Export failed: ${copyErr.message}`;
          return;
        }
        if (prevEl) {
          prevEl.classList.remove("hidden");
          prevEl.innerHTML = `<strong>Privacy-Redacted Report (review before posting):</strong><pre style="margin-top:6px;">${escapeHtml(rep.markdown)}</pre>`;
        }
        try {
          if (navigator.clipboard && navigator.clipboard.writeText) {
            await navigator.clipboard.writeText(rep.markdown);
            if (statusEl) statusEl.textContent = "Copied redacted report to clipboard!";
          } else if (statusEl) {
            statusEl.textContent = "Clipboard unavailable. Copy from the redacted preview below.";
          }
        } catch (_clipErr) {
          if (statusEl) statusEl.textContent = "Clipboard permission denied. Copy from the redacted preview below.";
        }
        return;
      }
      if (t.dataset.missedOpen && isSafeHttpUrl(t.dataset.missedOpen)) {
        window.open(t.dataset.missedOpen, "_blank", "noopener,noreferrer");
        return;
      }
      if (t.dataset.markUnsubIdx !== undefined) {
        const idx = Number(t.dataset.markUnsubIdx);
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
          ? "Marked unsubscribed & archived by user"
          : "Marked unsubscribed by user";
        qs("snapshot-modal").classList.add("hidden");
        renderJobResults(state.lastJobResults);
        await loadState(false);
        return;
      }
      if (t.dataset.missedMarkUnsub) {
        const mid = Number(t.dataset.missedMarkUnsub);
        const resp = await api("/api/unsub/mark-succeeded", {
          method: "POST",
          body: JSON.stringify({ missed_id: mid, archive: true }),
        });
        qs("snapshot-modal").classList.add("hidden");
        renderJobResults([
          {
            sender: `Missed #${mid}`,
            action: "archive_and_unsub",
            status: "ok",
            detail: resp.archived
              ? "Marked as unsubscribed & archived from Inbox"
              : "Marked as unsubscribed",
          },
        ]);
        await loadState(false);
        return;
      }
      if (t.dataset.missedResolve) {
        await api(`/api/missed/${Number(t.dataset.missedResolve)}/resolve`, {
          method: "POST",
          body: JSON.stringify({ note: "Resolved from UI" }),
        });
        await loadState(false);
        return;
      }
      if (t.dataset.missedRetry) {
        const res = await api(`/api/missed/${Number(t.dataset.missedRetry)}/retry`, {
          method: "POST",
          body: JSON.stringify({ headless: !qs("show-browser-toggle").checked }),
        });
        renderJobResults([
          {
            sender: `Missed #${t.dataset.missedRetry}`,
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
    const t = ev.target;
    if (!(t instanceof HTMLElement)) return;
    try {
      if (t.id === "master-cb" && t instanceof HTMLInputElement) {
        for (const item of filteredItems()) {
          if (t.checked) state.selectedUids.add(item.uid);
          else state.selectedUids.delete(item.uid);
        }
        render();
        return;
      }
      if (t.classList.contains("row-cb") && t instanceof HTMLInputElement && t.dataset.uid) {
        if (t.checked) state.selectedUids.add(t.dataset.uid);
        else state.selectedUids.delete(t.dataset.uid);
        render();
        return;
      }
      if (t.id === "unread-only-toggle" && t instanceof HTMLInputElement) {
        state.unreadOnly = t.checked;
        render();
        return;
      }
      if (t.classList.contains("pref-select") && t instanceof HTMLSelectElement && t.value) {
        const targetEmail = (t.dataset.prefEmail || "").toLowerCase();
        await api("/api/preferences", {
          method: "POST",
          body: JSON.stringify({
            sender_email: t.dataset.prefEmail || "",
            sender_domain: t.dataset.prefDom || "",
            display_name: t.dataset.prefName || "",
            preference: t.value,
          }),
        });
        t.value = "";
        // Clear cached row actions for this sender so the updated default takes effect immediately
        for (const item of state.data?.items || []) {
          if (item.header.sender_email.toLowerCase() === targetEmail) {
            state.actionsByUid.delete(item.uid);
          }
        }
        await loadState(false);
        return;
      }
      if (t.id === "rules-master-cb" && t instanceof HTMLInputElement) {
        for (const row of state.rulesItems || []) {
          if (t.checked) state.rulesSelectedKeys.add(row.sender_key);
          else state.rulesSelectedKeys.delete(row.sender_key);
        }
        renderSenderRulesRowsOnly();
        return;
      }
      if (t.classList.contains("rules-row-cb") && t instanceof HTMLInputElement && t.dataset.senderKey) {
        if (t.checked) state.rulesSelectedKeys.add(t.dataset.senderKey);
        else state.rulesSelectedKeys.delete(t.dataset.senderKey);
        renderSenderRulesRowsOnly();
        return;
      }
      if (t.classList.contains("rule-edit-select") && t instanceof HTMLSelectElement && t.value && t.dataset.ruleKey) {
        const key = t.dataset.ruleKey;
        const act = t.value;
        t.value = "";
        await applySenderMemoryUpdate([key], act);
        const nextStatus = qs("rules-status");
        if (nextStatus) {
          nextStatus.className = "modal-status ok";
          nextStatus.textContent = `Updated ${key}.`;
        }
        return;
      }
      if (t.id === "audit-run-select" && t instanceof HTMLSelectElement) {
        state.auditRunId = t.value;
        await refreshAuditLogData({ rowsOnly: true });
        return;
      }
    } catch (err) {
      showError(err.message);
    }
  });

  document.addEventListener("input", async (ev) => {
    const t = ev.target;
    if (t instanceof HTMLInputElement && t.id === "rules-search-input") {
      state.rulesSearch = t.value;
      try {
        await refreshSenderRulesData({ rowsOnly: true });
      } catch (err) {
        showError(err.message);
      }
      return;
    }
    if (t instanceof HTMLInputElement && t.id === "audit-search-input") {
      state.auditSearch = t.value;
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
    openConfirmation("Confirm Selected Actions", nonNoopPairs(items)).catch((e) => showError(e.message));
  });
  qs("btn-exec-category").addEventListener("click", () => {
    const catLabel = CATEGORIES.find((c) => c.id === state.category)?.label || "Category";
    openConfirmation(`Confirm ${catLabel} Actions`, nonNoopPairs(filteredItems())).catch((e) => showError(e.message));
  });
  qs("btn-exec-all").addEventListener("click", () => {
    openConfirmation("Confirm All Queued Actions", nonNoopPairs(state.data?.items || [])).catch((e) => showError(e.message));
  });
  qs("btn-tray-sweep").addEventListener("click", () => openTrayConfirmation().catch((e) => showError(e.message)));
  qs("btn-tray-dismiss").addEventListener("click", () => {
    const tray = state.data?.tray || [];
    if (!tray.length) return;
    const uids = tray.map((t) => t.message_uid);
    qs("modal-title").textContent = `Confirm Dismiss Safety Tray (${tray.length} items)`;
    qs("modal-body").innerHTML = `
      <div class="confirm-warning warn">
        Dismissing the Safety Tray clears <strong>${tray.length}</strong> opened item(s) from this reminder tray only. Your emails will remain untouched in Mail.app Inbox.
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

  loadState(false).catch((err) => {
    showError(err.message);
  });
})();
