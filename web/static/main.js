let timelineChart, breakdownChart, page = 1, categories = [];

const fmtEUR = (v) => (v >= 0 ? "+" : "") + v.toLocaleString("de-DE", {style: "currency", currency: "EUR"});
const $ = (id) => document.getElementById(id);

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function showError(msg) {
  const b = $("error-banner");
  b.textContent = msg;
  b.className = "fixed bottom-4 left-1/2 -translate-x-1/2 bg-red-600 text-white px-4 py-2 rounded-lg shadow";
  setTimeout(() => b.classList.add("hidden"), 5000);
}

function toast(msg) {
  const b = $("error-banner");
  b.textContent = msg;
  b.className = "fixed bottom-4 left-1/2 -translate-x-1/2 bg-green-600 text-white px-4 py-2 rounded-lg shadow";
  setTimeout(() => b.classList.add("hidden"), 3000);
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    const detail = (await r.json().catch(() => ({}))).detail || r.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return r.json();
}

const JSON_HEADERS = {"Content-Type": "application/json"};

async function loadKpis() {
  const k = await api("/api/kpis");
  $("kpi-balance").textContent = fmtEUR(k.total_balance);
  $("kpi-income").textContent = fmtEUR(k.income_month);
  $("kpi-expenses").textContent = fmtEUR(k.expenses_month);
  $("kpi-net").textContent = fmtEUR(k.net_cashflow);
}

async function loadTimeline(granularity = "monthly") {
  const data = await api(`/api/timeline?granularity=${granularity}`);
  const labels = data.map(d => d.period);
  const income = data.map(d => d.income);
  const expenses = data.map(d => Math.abs(d.expenses));
  if (timelineChart) {
    timelineChart.data.labels = labels;
    timelineChart.data.datasets[0].data = income;
    timelineChart.data.datasets[1].data = expenses;
    timelineChart.update();
    return;
  }
  timelineChart = new Chart($("timeline-chart"), {
    type: "bar",
    data: {
      labels,
      datasets: [
        {label: "Einnahmen", data: income, backgroundColor: "rgba(22,163,74,0.55)", grouped: false},
        {label: "Ausgaben", data: expenses, backgroundColor: "#dc2626", borderColor: "#dc2626", borderWidth: 1, grouped: false, order: 1},
      ],
    },
    options: {scales: {x: {stacked: false}, y: {stacked: false}}},
  });
}

function palette(n) {
  const colors = ["#2563eb", "#16a34a", "#dc2626", "#d97706", "#7c3aed", "#0d9488", "#db2777", "#65a30d", "#57534e"];
  return Array.from({length: n}, (_, i) => colors[i % colors.length]);
}

async function loadBreakdown() {
  const data = await api("/api/categories/breakdown");
  const total = data.reduce((s, d) => s + d.total, 0);
  const labels = data.map(d => d.category);
  const values = data.map(d => d.total);
  if (!breakdownChart) {
    breakdownChart = new Chart($("breakdown-chart"), {
      type: "doughnut",
      data: {labels, datasets: [{data: values, backgroundColor: palette(data.length)}]},
      options: {plugins: {tooltip: {callbacks: {label: (c) =>
        `${c.label}: ${fmtEUR(c.parsed)} (${total ? Math.round(c.parsed / total * 100) : 0}%)`}}}},
    });
  } else {
    breakdownChart.data.labels = labels;
    breakdownChart.data.datasets[0].data = values;
    breakdownChart.data.datasets[0].backgroundColor = palette(data.length);
    breakdownChart.update();
  }
}

async function loadTransactions() {
  const params = new URLSearchParams({search: $("f-search").value, page});
  const cat = $("f-category").value;
  const acct = $("f-account").value;
  const from = $("f-from").value;
  const to = $("f-to").value;
  if (cat) params.set("category_id", cat);
  if (acct) params.set("account_id", acct);
  if (from) params.set("date_from", from);
  if (to) params.set("date_to", to);

  const data = await api(`/api/transactions?${params}`);
  const body = $("tx-body");
  body.innerHTML = "";
  for (const t of data.items) {
    const tr = document.createElement("tr");
    tr.className = "border-b hover:bg-slate-50";
    tr.innerHTML = `
      <td class="py-2 whitespace-nowrap">${escapeHtml(t.buchungsdatum)}</td>
      <td class="max-w-32 truncate sm:max-w-none">${escapeHtml(t.partner_name)}</td>
      <td class="hidden sm:table-cell text-slate-500">${escapeHtml((t.verwendungszweck ?? "").slice(0, 40))}</td>
      <td class="text-right font-mono whitespace-nowrap ${t.betrag < 0 ? "text-red-600" : "text-green-600"}">${fmtEUR(t.betrag)}</td>
      <td class="hidden sm:table-cell text-slate-500 text-xs">${escapeHtml(t.account_bank)}</td>
      <td><select class="cat-select border rounded text-xs px-1 py-2 max-w-28" data-tx="${t.id}">
        <option value="">Nicht zugeordnet</option>
        ${categories.map(c => `<option value="${c.id}" ${c.id === t.category_id ? "selected" : ""}>${escapeHtml(c.name)}</option>`).join("")}
      </select></td>`;
    body.appendChild(tr);
  }
  $("tx-count").textContent = `${data.total} Transaktionen · Seite ${page}`;
  body.querySelectorAll(".cat-select").forEach((sel) => {
    sel.addEventListener("change", async () => {
      try {
        await api(`/api/transactions/${sel.dataset.tx}/category`, {
          method: "PATCH", headers: JSON_HEADERS,
          body: JSON.stringify({category_id: sel.value ? +sel.value : null}),
        });
        toast("Kategorie gespeichert");
        loadBreakdown();
      } catch (e) { showError(e.message); }
    });
  });
}

async function loadCategories() {
  categories = await api("/api/categories");
  const filter = $("f-category");
  const cur = filter.value;
  filter.innerHTML = '<option value="">Alle Kategorien</option>' +
    categories.map(c => `<option value="${c.id}">${escapeHtml(c.name)}</option>`).join("");
  filter.value = cur;

  const ul = $("cat-list");
  ul.innerHTML = categories.map(c =>
    `<li class="flex justify-between py-1"><span>${escapeHtml(c.name)} <span class="text-slate-400 text-xs">${c.type === "income" ? "Einnahme" : "Ausgabe"}</span></span>
     <button data-id="${c.id}" class="del-cat text-red-500 text-xs">✕</button></li>`).join("");
  ul.querySelectorAll(".del-cat").forEach(b => b.addEventListener("click", async () => {
    if (!confirm("Kategorie löschen? Zugeordnete Umsätze werden 'Nicht zugeordnet'.")) return;
    try {
      await api(`/api/categories/${b.dataset.id}`, {method: "DELETE"});
      await refreshCategoriesAndData();
    } catch (e) { showError(e.message); }
  }));
}

async function loadAccounts() {
  const accounts = await api("/api/accounts");
  const sel = $("f-account");
  const cur = sel.value;
  sel.innerHTML = '<option value="">Alle Konten</option>' +
    accounts.map(a => `<option value="${escapeHtml(a.id)}">${escapeHtml(a.bank_name)} …${escapeHtml(a.iban.slice(-4))}</option>`).join("");
  sel.value = cur;
}

async function refreshCategoriesAndData() {
  await loadCategories();
  await Promise.all([loadTransactions(), loadBreakdown()]);
}

$("add-cat").addEventListener("click", async () => {
  const name = $("new-cat-name").value.trim();
  const type = $("new-cat-type").value;
  if (!name) return showError("Bitte einen Namen eingeben");
  try {
    await api("/api/categories", {method: "POST", headers: JSON_HEADERS, body: JSON.stringify({name, type})});
    $("new-cat-name").value = "";
    await loadCategories();
  } catch (e) { showError(e.message); }
});

document.querySelectorAll(".gran-btn").forEach(b => b.addEventListener("click", () => {
  document.querySelectorAll(".gran-btn").forEach(x =>
    x.className = "gran-btn px-3 py-1 text-sm rounded bg-slate-200 text-slate-700");
  b.className = "gran-btn px-3 py-1 text-sm rounded bg-blue-600 text-white";
  loadTimeline(b.dataset.gran).catch(e => showError(e.message));
}));

$("f-apply").addEventListener("click", () => { page = 1; loadTransactions().catch(e => showError(e.message)); });
$("f-search").addEventListener("keydown", (e) => { if (e.key === "Enter") $("f-apply").click(); });
$("prev-page").addEventListener("click", () => { if (page > 1) { page--; loadTransactions().catch(e => showError(e.message)); } });
$("next-page").addEventListener("click", () => { page++; loadTransactions().catch(e => showError(e.message)); });

// ---------- Bankabruf (FinTS) ----------
// Die PIN wird nur in das Eingabefeld geschrieben, beim Absenden geleert und nie gespeichert.
// TAN-/Auswahl-Eingaben werden ebenfalls sofort verworfen.
let syncJob = null, syncPoll = null, lastPromptKey = null;

function setSyncStatus(text, cls = "text-slate-600") {
  const el = $("sync-status");
  el.textContent = text;
  el.className = `text-sm mt-3 ${cls}`;
}

function hidePrompt() {
  const box = $("sync-prompt");
  box.classList.add("hidden");
  box.innerHTML = "";
}

function renderPrompt(job) {
  const box = $("sync-prompt");
  const p = job.prompt;
  if (!p) return hidePrompt();
  box.classList.remove("hidden");
  if (job.status === "tan_required") {
    const img = p.image ? `<img src="${p.image}" alt="photoTAN" class="my-2 max-w-xs border bg-white">` : "";
    const text = escapeHtml(p.text || "");
    if (p.decoupled) {
      box.innerHTML = `
        <div class="mb-2">${text || "Bitte bestätige den Abruf in deiner Banking-App."}</div>
        ${img}
        <button id="sync-ok" class="px-4 py-2 bg-blue-600 text-white rounded text-sm">Bestätigt</button>
        <button id="sync-cancel" class="px-4 py-2 border rounded text-sm ml-2">Abbrechen</button>`;
      $("sync-ok").onclick = () => sendSyncInput({});
    } else {
      box.innerHTML = `
        <div class="mb-2">${text || "Bitte TAN eingeben."}</div>
        ${img}
        <div class="flex gap-2 items-end">
          <input id="sync-tan" type="text" inputmode="numeric" autocomplete="one-time-code" placeholder="TAN" class="border rounded px-2 py-2 text-sm">
          <button id="sync-ok" class="px-4 py-2 bg-blue-600 text-white rounded text-sm">Senden</button>
          <button id="sync-cancel" class="px-4 py-2 border rounded text-sm">Abbrechen</button>
        </div>`;
      $("sync-ok").onclick = () => sendSyncInput({tan: $("sync-tan").value});
      $("sync-tan").focus();
    }
  } else if (job.status === "choice_required") {
    box.innerHTML = `
      <div class="mb-2">${escapeHtml(p.text)}</div>
      <div class="flex gap-2 items-end">
        <select id="sync-choice" class="border rounded px-2 py-2 text-sm">
          ${p.options.map(o => `<option value="${o.index}">${escapeHtml(o.label)}</option>`).join("")}
        </select>
        <button id="sync-ok" class="px-4 py-2 bg-blue-600 text-white rounded text-sm">Weiter</button>
        <button id="sync-cancel" class="px-4 py-2 border rounded text-sm">Abbrechen</button>
      </div>`;
    $("sync-ok").onclick = () => sendSyncInput({index: +$("sync-choice").value});
  }
  const cancel = $("sync-cancel");
  if (cancel) cancel.onclick = () => cancelSync();
}

async function sendSyncInput(value) {
  if (!syncJob) return;
  // Eingaben sofort leeren, bevor sie abgeschickt werden
  const tanField = $("sync-tan");
  if (tanField) tanField.value = "";
  try {
    await api(`/api/sync/${syncJob.job_id}/input`, {method: "POST", headers: JSON_HEADERS, body: JSON.stringify(value)});
    hidePrompt();
    setSyncStatus("Eingabe gesendet…");
  } catch (e) { showError(e.message); }
}

async function cancelSync() {
  if (!syncJob) return;
  try {
    await api(`/api/sync/${syncJob.job_id}/cancel`, {method: "POST"});
  } catch (e) { showError(e.message); }
}

function syncSummary(result) {
  const accts = Object.entries(result.accounts || {});
  if (!accts.length) return "Keine Konten gefunden.";
  return accts.map(([iban, r]) =>
    `…${iban.slice(-4)}: ${r.new} neu, ${r.skipped} bereits vorhanden`).join(" · ");
}

function pollSync() {
  clearInterval(syncPoll);
  syncPoll = setInterval(async () => {
    if (!syncJob) return clearInterval(syncPoll);
    try {
      const job = await api(`/api/sync/${syncJob.job_id}`);
      syncJob = job;
      if (job.status === "done") {
        clearInterval(syncPoll);
        hidePrompt();
        setSyncStatus(`Abruf abgeschlossen – ${syncSummary(job.result)}`, "text-green-700");
        $("sync-start").disabled = false;
        refreshData();
      } else if (job.status === "error") {
        clearInterval(syncPoll);
        hidePrompt();
        setSyncStatus(`Fehler: ${job.error}`, "text-red-600");
        $("sync-start").disabled = false;
      } else if (job.status === "tan_required" || job.status === "choice_required") {
        setSyncStatus(job.status === "choice_required" ? "Auswahl nötig" : "TAN / Freigabe nötig", "text-amber-700");
        // Nur neu zeichnen, wenn sich der Prompt ändert – sonst geht eine gerade getippte TAN verloren
        const key = job.status + JSON.stringify(job.prompt);
        if (key !== lastPromptKey) { lastPromptKey = key; renderPrompt(job); }
      } else {
        setSyncStatus("Verbinde mit der Bank…");
      }
    } catch (e) {
      clearInterval(syncPoll);
      $("sync-start").disabled = false;
      setSyncStatus(`Fehler: ${e.message}`, "text-red-600");
    }
  }, 1500);
}

$("sync-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const pinField = $("sync-pin");
  const pin = pinField.value;
  pinField.value = "";
  lastPromptKey = null;            // PIN sofort aus dem Feld entfernen
  if (!pin) return showError("Bitte PIN eingeben");
  if (!$("sync-bank").value) return showError("Keine konfigurierte Bank – siehe .env.example");
  $("sync-start").disabled = true;
  setSyncStatus("Starte Abruf…");
  try {
    syncJob = await api("/api/sync", {
      method: "POST", headers: JSON_HEADERS,
      body: JSON.stringify({bank: $("sync-bank").value, pin, days: +$("sync-days").value || 100}),
    });
    pollSync();
  } catch (e) {
    $("sync-start").disabled = false;
    setSyncStatus(`Fehler: ${e.message}`, "text-red-600");
  }
});

async function loadSyncBanks() {
  const banks = await api("/api/sync/banks");
  const sel = $("sync-bank");
  sel.innerHTML = banks.map(b =>
    `<option value="${b.key}" ${b.configured ? "" : "disabled"}>${escapeHtml(b.label)}${b.configured ? "" : " (nicht in .env konfiguriert)"}</option>`).join("");
  // Erste konfigurierte Bank vorauswählen; ohne konfigurierte Bank Abruf sperren
  const first = banks.find(b => b.configured);
  if (first) sel.value = first.key;
  $("sync-start").disabled = !first;
  if (!first) setSyncStatus("Keine Bank in der .env konfiguriert – siehe .env.example", "text-amber-700");
}

function refreshData() {
  loadKpis().catch(e => showError(e.message));
  loadTimeline(document.querySelector(".gran-btn.bg-blue-600")?.dataset.gran || "monthly").catch(e => showError(e.message));
  loadAccounts().catch(e => showError(e.message));
  refreshCategoriesAndData().catch(e => showError(e.message));
}

async function init() {
  try {
    await Promise.all([loadKpis(), loadTimeline("monthly"), loadAccounts(), loadSyncBanks()]);
    await refreshCategoriesAndData();
  } catch (e) { showError(e.message); }
}

init();
