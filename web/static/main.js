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

// Aktuell im Filter gewähltes Konto -> Query-Fragment für die Chart-Endpunkte.
function accountQuery(prefix = "?") {
  const acct = $("f-account").value;
  return acct ? `${prefix}account_id=${encodeURIComponent(acct)}` : "";
}

function currentGranularity() {
  return document.querySelector(".gran-btn.bg-blue-600")?.dataset.gran || "monthly";
}

// Konto-Filter wirkt auf Kennzahlen, Timeline und Kategorien-Grafik.
async function loadCharts() {
  await Promise.all([loadKpis(), loadTimeline(currentGranularity()), loadBreakdown()]);
}

async function loadKpis() {
  const k = await api(`/api/kpis${accountQuery()}`);
  $("kpi-balance").textContent = fmtEUR(k.total_balance);
  $("kpi-income").textContent = fmtEUR(k.income_month);
  $("kpi-expenses").textContent = fmtEUR(k.expenses_month);
  $("kpi-net").textContent = fmtEUR(k.net_cashflow);
}

async function loadTimeline(granularity = "monthly") {
  const data = await api(`/api/timeline?granularity=${granularity}${accountQuery("&")}`);
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
  const data = await api(`/api/categories/breakdown${accountQuery()}`);
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
      const txId = sel.dataset.tx;
      const catId = sel.value ? +sel.value : null;
      try {
        let res = await api(`/api/transactions/${txId}/category`, {
          method: "PATCH", headers: JSON_HEADERS,
          body: JSON.stringify({category_id: catId}),
        });
        // Backend meldet, wie viele gleichartige Umsätze eine ABWEICHENDE
        // Kategorie haben. Erst nach Bestätigung werden auch diese überschrieben.
        if (catId && res.affected > 0) {
          const ok = confirm(
            `${res.affected} weitere Umsätze mit gleichem Empfänger und ` +
            `Verwendungszweck haben eine andere Kategorie.\n\n` +
            `Auch auf "${res.category}" setzen?`
          );
          if (ok) {
            res = await api(`/api/transactions/${txId}/category`, {
              method: "PATCH", headers: JSON_HEADERS,
              body: JSON.stringify({category_id: catId, apply_to_all: true}),
            });
            toast(`"${res.category}" auf ${res.updated} Umsätze angewandt`);
          } else {
            toast("Nur dieser Umsatz geändert");
          }
        } else {
          const n = res.updated ?? 1;
          toast(n > 1
            ? `Kategorie gespeichert · auf ${n} Umsätze angewandt`
            : "Kategorie gespeichert");
        }
        loadTransactions();
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

$("auto-cat").addEventListener("click", async () => {
  const btn = $("auto-cat");
  btn.disabled = true;
  try {
    const r = await api("/api/categorize", {method: "POST", headers: JSON_HEADERS, body: "{}"});
    toast(`${r.changed} von ${r.scanned} Umsätzen automatisch zugeordnet`);
    await refreshCategoriesAndData();
  } catch (e) {
    showError(e.message);
  } finally {
    btn.disabled = false;
  }
});

document.querySelectorAll(".gran-btn").forEach(b => b.addEventListener("click", () => {
  document.querySelectorAll(".gran-btn").forEach(x =>
    x.className = "gran-btn px-3 py-1 text-sm rounded bg-slate-200 text-slate-700");
  b.className = "gran-btn px-3 py-1 text-sm rounded bg-blue-600 text-white";
  loadTimeline(b.dataset.gran).catch(e => showError(e.message));
}));

// Kontowechsel aktualisiert Tabelle UND alle Grafiken – die Kennzahlen,
// die Timeline und der Kategorien-Donut beziehen sich dann nur auf dieses Konto.
$("f-account").addEventListener("change", () => {
  page = 1;
  Promise.all([loadCharts(), loadTransactions()]).catch(e => showError(e.message));
});

$("f-apply").addEventListener("click", () => { page = 1; loadTransactions().catch(e => showError(e.message)); });
$("f-search").addEventListener("keydown", (e) => { if (e.key === "Enter") $("f-apply").click(); });
$("prev-page").addEventListener("click", () => { if (page > 1) { page--; loadTransactions().catch(e => showError(e.message)); } });
$("next-page").addEventListener("click", () => { page++; loadTransactions().catch(e => showError(e.message)); });

// ---------- Bankabruf (FinTS) ----------
// Die PIN wird nur in das Eingabefeld geschrieben, beim Absenden geleert und nie gespeichert.
// TAN-/Auswahl-Eingaben werden ebenfalls sofort verworfen.
let syncJob = null, syncPoll = null, lastPromptKey = null;
let currentPrompt = null;   // momentan angezeigtes photoTAN-Prompt (für Modal/Nebenfunktionen)

// --- photoTAN-Grafik Modal ---
// „Bild teilen“ nur aktivieren, wenn navigator.share + canShare mit Dateien verfügbar sind
const challengeCanShareFiles = (() => {
  if (typeof navigator.share !== "function" || typeof navigator.canShare !== "function") return false;
  try { return !!navigator.canShare({files: [new File(["x"], "x.png")]}); } catch { return false; }
})();
let challengeZoom = 1, challengeNatW = 0;

function challengeUrl(p) { return (p && (p.image_url || p.image)) || ""; }

function updateChallengeZoom() {
  const img = $("challenge-img");
  if (!img) return;
  if (challengeZoom === 1) {
    img.style.maxHeight = "80vh";
    img.style.maxWidth = "100%";
    img.style.width = "";
  } else {
    img.style.maxHeight = "none";
    img.style.maxWidth = "none";
    img.style.width = (challengeNatW * challengeZoom) + "px";
  }
}

function openImageModal() {
  const url = challengeUrl(currentPrompt);
  if (!url) return;
  const modal = $("challenge-modal");
  challengeZoom = 1; challengeNatW = 0;
  const img = $("challenge-img");
  img.onload = () => { challengeNatW = img.naturalWidth || img.width || 0; updateChallengeZoom(); };
  img.onerror = null;
  img.src = url;
  updateChallengeZoom();
  $("challenge-share").classList.toggle("hidden", !challengeCanShareFiles);
  modal.classList.remove("hidden");
  document.body.style.overflow = "hidden";
}

function closeImageModal() {
  const m = $("challenge-modal");
  if (!m || m.classList.contains("hidden")) return;
  m.classList.add("hidden");
  document.body.style.overflow = "";
}

async function shareChallenge() {
  const url = challengeUrl(currentPrompt);
  if (!url) return;
  try {
    let blob;
    if (url.startsWith("data:")) {
      const m = /^data:([^;,]*)(;base64)?,(.*)$/.exec(url);
      if (!m) throw new Error("Ungültige Bilddaten");
      blob = m[2]
        ? (() => { const bin = atob(m[3]); const arr = new Uint8Array(bin.length); for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i); return new Blob([arr], {type: m[1] || "image/png"}); })()
        : new Blob([decodeURIComponent(m[3])], {type: m[1] || "image/png"});
    } else {
      const r = await fetch(url);
      if (!r.ok) throw new Error("Bild konnte nicht geladen werden");
      blob = await r.blob();
    }
    const ext = (blob.type.split("/")[1] || "png").split("+")[0] || "png";
    const seq = currentPrompt && typeof currentPrompt.seq !== "undefined" ? currentPrompt.seq + "_" : "";
    const file = new File([blob], `photoTAN_${seq}challenge.${ext}`, {type: blob.type});
    await navigator.share({files: [file], title: "photoTAN-Challenge", text: currentPrompt && currentPrompt.text ? currentPrompt.text : "photoTAN scannen"});
  } catch (e) {
    if (e && (e.name === "AbortError" || e.name === "NotAllowedError")) return; // Nutzer hat abgebrochen
    showError("Bild konnte nicht geteilt werden: " + (e && e.message ? e.message : "unbekannter Fehler"));
  }
}

function setSyncStatus(text, cls = "text-slate-600") {
  const el = $("sync-status");
  el.textContent = text;
  el.className = `text-sm mt-3 ${cls}`;
}

function hidePrompt() {
  closeImageModal();
  currentPrompt = null;
  const box = $("sync-prompt");
  box.classList.add("hidden");
  box.innerHTML = "";
}

function renderPrompt(job) {
  const box = $("sync-prompt");
  const p = job.prompt;
  if (!p) return hidePrompt();
  box.classList.remove("hidden");
  currentPrompt = p;
  if (job.status === "tan_required") {
    const imgSrc = challengeUrl(p);
    const img = imgSrc ? `<img src="${imgSrc}" alt="photoTAN" class="my-2 max-w-xs border bg-white" style="image-rendering:pixelated">` : "";
    const actions = img ? `
      <div class="flex flex-wrap gap-2 mt-2">
        <button id="sync-zoom" type="button" class="px-3 py-1.5 bg-blue-600 text-white rounded text-sm">Grafik groß anzeigen</button>
        <button id="sync-newtab" type="button" class="px-3 py-1.5 border rounded text-sm">In neuem Tab öffnen</button>
      </div>` : "";
    const text = escapeHtml(p.text || "");
    if (p.decoupled) {
      box.innerHTML = `
        <div class="mb-2">${text || "Bitte bestätige den Abruf in deiner Banking-App."}</div>
        ${img}${actions}
        <button id="sync-ok" class="px-4 py-2 bg-blue-600 text-white rounded text-sm">Bestätigt</button>
        <button id="sync-cancel" class="px-4 py-2 border rounded text-sm ml-2">Abbrechen</button>`;
      $("sync-ok").onclick = () => sendSyncInput({});
    } else {
      box.innerHTML = `
        <div class="mb-2">${text || "Bitte TAN eingeben."}</div>
        ${img}${actions}
        <div class="flex gap-2 items-end">
          <input id="sync-tan" type="text" inputmode="numeric" autocomplete="one-time-code" placeholder="TAN" class="border rounded px-2 py-2 text-sm">
          <button id="sync-ok" class="px-4 py-2 bg-blue-600 text-white rounded text-sm">Senden</button>
          <button id="sync-cancel" class="px-4 py-2 border rounded text-sm">Abbrechen</button>
        </div>`;
      $("sync-ok").onclick = () => sendSyncInput({tan: $("sync-tan").value});
      $("sync-tan").focus();
    }
    const zb = $("sync-zoom"); if (zb) zb.onclick = () => openImageModal();
    const nb = $("sync-newtab"); if (nb) nb.onclick = () => { const u = challengeUrl(p); if (u) window.open(u, "_blank", "noopener"); };
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
        // Nur neu zeichnen, wenn sich der Prompt ändert – sonst geht eine gerade getippte TAN verloren.
        // Bevorzugt job.prompt.seq (fortlaufende Nummer), damit auch text-identische Folge-Challenges neu gezeichnet werden.
        const key = job.prompt && typeof job.prompt.seq !== "undefined"
          ? "seq:" + job.prompt.seq
          : job.status + JSON.stringify(job.prompt);
        if (key !== lastPromptKey) {
          lastPromptKey = key;
          currentPrompt = job.prompt;
          closeImageModal();   // veraltete Grafik aus dem Modal entfernen, wenn eine neue Challenge kommt
          renderPrompt(job);
        }
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

function initImageModal() {
  const modal = $("challenge-modal");
  if (!modal) return;
  $("challenge-close").onclick = closeImageModal;
  $("challenge-backdrop").onclick = closeImageModal;
  $("challenge-newtab").onclick = () => { const u = challengeUrl(currentPrompt); if (u) window.open(u, "_blank", "noopener"); };
  $("challenge-share").onclick = () => { shareChallenge().catch(() => {}); };
  const zoom = $("challenge-zoom");
  zoom.textContent = "2x";
  zoom.onclick = () => {
    challengeZoom = challengeZoom === 1 ? 2 : 1;
    zoom.textContent = challengeZoom === 1 ? "2x" : "1x";
    updateChallengeZoom();
  };
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeImageModal(); });
}

function refreshData() {
  loadCharts().catch(e => showError(e.message));
  loadAccounts().catch(e => showError(e.message));
  refreshCategoriesAndData().catch(e => showError(e.message));
}

async function init() {
  initImageModal();
  try {
    await Promise.all([loadKpis(), loadTimeline("monthly"), loadAccounts(), loadSyncBanks()]);
    await refreshCategoriesAndData();
  } catch (e) { showError(e.message); }
}

init();
