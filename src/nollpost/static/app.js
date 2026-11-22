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
    const acctSuffix = acctParts.length ? ` — ${acctParts.join(" · ")}` : "";
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
            <strong>${escapeHtml(m.sender)}</strong> — ${escapeHtml(m.subject)}
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
        .map((d) => `• ${escapeHtml(d.sender)} — ${escapeHtml(d.subject)} → <code>${escapeHtml(d.url)}</code>`)
        .join("<br/>")}</div>`;
    }
    if ((preview.draft_declines || []).length) {
      detailsHtml += `<div class="preview-box"><strong>Boilerplate Drafts in Mail.app (kept in Inbox):</strong><br/>${preview.draft_declines
        .map((d) => `• ${escapeHtml(d.sender)} — ${escapeHtml(d.subject)}`)
        .join("<br/>")}</div>`;
    }
    if ((preview.send_declines || []).length) {
      detailsHtml += `<div class="preview-box"><strong>Auto-Send Boilerplate Replies &amp; Archive:</strong><br/>${preview.send_declines
        .map((d) => `• ${escapeHtml(d.sender)} — ${escapeHtml(d.subject)}`)
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
          <div><span class="pill ${cls}">${escapeHtml(r.status)}</span> <strong>${escapeHtml(r.sender)}</strong> — ${escapeHtml(r.detail)}</div>
          <div style="display:flex;gap:6px;flex-wrap:wrap;">${btns.join("")}</div>
        </div>`;
      })
      .join("");
    resEl.innerHTML = `<div style="display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:6px;margin-bottom:6px;"><strong>Last Run Results (${results.length})</strong><span style="font-size:11px;color:var(--text-secondary);">Note: Results and screenshots reset when you start the next run.</span></div>${warnRow}${rows}`;
  }

  function showProofModal(idx) {
    const r = state.lastJobResults[idx];
    if (!r) return;
    qs("snap-title").textContent = `Execution Proof — ${r.sender}`;
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
          <div>• [${escapeHtml(i.account)}] <strong>${escapeHtml(i.subject)}</strong> — ${escapeHtml(i.sender)}${changedTag}${urlLine}</div>
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
    qs("snap-title").textContent = `Missed Unsubscribe #${entry.id} — ${entry.sender}`;
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
      <div class="preview-box"><strong>Reason:</strong> ${escapeHtml(entry.failure_reason)}<br/><strong>Mail ID:</strong> ${escapeHtml(entry.account)}/${escapeHtml(entry.mailbox)} #${escapeHtml(entry.internal_id)}${finalUrlHtml}<div style="margin-top:8px;"><button class="btn small primary" data-missed-mark-unsub="${entry.id}">Mark as Unsubscribed &amp; Archive</button></div></div>
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

  document.addEventListener("click", async (ev) => {
    const t = ev.target;
    if (!(t instanceof HTMLElement)) return;
    try {
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
    } catch (err) {
      showError(err.message);
    }
  });

  qs("search-input").addEventListener("input", (ev) => {
    state.search = ev.target.value;
    render();
  });
  qs("refresh-btn").addEventListener("click", () => loadState(true).catch((e) => showError(e.message)));
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
