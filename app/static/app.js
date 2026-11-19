// ipa-source web admin – plain ES module, talks only to /api/v1 (same API a native app can use)
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const view = $("#view");
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const CATEGORIES = ["developer", "entertainment", "games", "lifestyle", "other", "photo-video", "social", "utilities"];

// ------------------------------------------------------------------ api --
async function api(method, path, body, opts = {}) {
  const headers = { "X-Requested-With": "ipa-source" };
  let payload = body;
  if (body !== undefined && !(body instanceof Blob) && !(body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  const res = await fetch("/api/v1" + path, { method, headers, body: payload, credentials: "same-origin" });
  let data = null;
  try { data = await res.json(); } catch { /* empty */ }
  if (res.status === 401 && !opts.allow401) { renderLogin(); throw new Error("Nicht angemeldet"); }
  if (!res.ok) throw new Error(data?.error?.message || `HTTP ${res.status}`);
  return data;
}

function toast(msg, error = false) {
  const d = document.createElement("div");
  d.textContent = msg; if (error) d.className = "error";
  $("#toast").append(d); setTimeout(() => d.remove(), error ? 6000 : 3000);
}
const guard = (fn) => async (...a) => { try { await fn(...a); } catch (e) { if (e.message !== "Nicht angemeldet") toast(e.message, true); } };
const fmtSize = (n) => n > 1e9 ? (n / 1e9).toFixed(2) + " GB" : n > 1e6 ? (n / 1e6).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1e3)) + " KB";
const fmtDate = (s) => s ? new Date(s).toLocaleString("de-DE", { dateStyle: "medium", timeStyle: "short" }) : "–";
const iconBadge = (a) => ({
  missing: '<span class="badge err">Kein Logo</span>', blank: '<span class="badge err">Leeres Logo</span>',
  custom: '<span class="badge upload">Eigenes Icon</span>', external: '<span class="badge">Externes Icon</span>',
}[a.icon_status] || "");
const originLabel = { release: "Release", artifact: "Artefakt", upload: "Upload", webdav: "WebDAV" };

// "Abbrechen"/"Schließen" must close without form validation (required fields would block it)
$("#modal-body").addEventListener("click", (e) => {
  if (e.target.closest('button[value="cancel"]')) { e.preventDefault(); $("#modal").close(); }
});
// Click on the dark backdrop closes the dialog
$("#modal").addEventListener("click", (e) => { if (e.target === $("#modal")) $("#modal").close(); });

function modal(html, onSubmit) {
  const dlg = $("#modal"), form = $("#modal-body");
  form.innerHTML = html;
  form.querySelectorAll('button[value="cancel"]').forEach((b) => { b.type = "button"; b.formNoValidate = true; });
  form.onsubmit = async (e) => {
    if (e.submitter?.value === "cancel") return;
    e.preventDefault();
    try { if ((await onSubmit?.(new FormData(form), form)) !== false) dlg.close(); }
    catch (err) { toast(err.message, true); }
  };
  dlg.showModal();
  return form;
}

function copyBox(value) {
  return `<div class="copy"><input readonly value="${esc(value)}"><button type="button" class="sm" data-copy="${esc(value)}">Kopieren</button></div>`;
}
document.addEventListener("click", (e) => {
  const v = e.target.closest("[data-copy]")?.dataset.copy;
  if (v) { navigator.clipboard?.writeText(v); toast("Kopiert"); }
});

// --------------------------------------------------------------- router --
const routes = {
  apps: renderApps, app: renderApp, sources: renderSources, github: renderGithubRepos,
  upload: renderUpload, feed: renderFeed, settings: renderSettings, log: renderLog, icon: renderIconDesigner, signing: renderSigning, downloads: renderDownloads,
};
let timer = null;
async function route() {
  clearInterval(timer);
  const [, name = "apps", arg] = location.hash.split("/");
  $$("nav a").forEach((a) => a.classList.toggle("active", a.dataset.nav === (["app", "icon"].includes(name) ? "apps" : name === "github" ? "sources" : name)));
  await guard((routes[name] || renderApps))(arg);
}

async function boot() {
  const info = await (await fetch("/api/v1/info")).json();
  if (info.setup_required) return renderSetup();
  const me = await api("GET", "/me", undefined, { allow401: true }).catch(() => null);
  if (!me) return renderLogin();
  $("#nav").hidden = false;
  pollWorker();
  route();
}
window.addEventListener("hashchange", route);
$("#logout").onclick = guard(async () => { await api("POST", "/auth/logout"); location.hash = ""; location.reload(); });

async function pollWorker() {
  try {
    const s = await api("GET", "/status");
    const w = s.worker;
    $("#worker").textContent = w.running ? `Sync läuft: ${w.running}` : `${s.counts.apps} Apps · ${s.counts.builds} Builds · ${fmtSize(s.counts.bytes)}`;
  } catch { /* ignore */ }
  setTimeout(pollWorker, 5000);
}

// ----------------------------------------------------------------- auth --
function renderSetup() {
  $("#nav").hidden = true;
  view.innerHTML = `<div class="auth panel stack">
    <h1>Einrichtung</h1><p class="muted">Lege den Admin-Benutzer an.</p>
    <form id="f" class="stack">
      <div><label>Benutzername</label><input name="username" value="admin" required autocomplete="username"></div>
      <div><label>Passwort (min. 8 Zeichen)</label><input name="password" type="password" minlength="8" required autocomplete="new-password"></div>
      <button class="primary">Anlegen</button></form></div>`;
  $("#f").onsubmit = guard(async (e) => {
    e.preventDefault();
    await api("POST", "/setup", Object.fromEntries(new FormData(e.target)));
    location.hash = "#/settings"; boot();
  });
}

function renderLogin() {
  $("#nav").hidden = true;
  view.innerHTML = `<div class="auth panel stack">
    <div class="brand"><span class="logo"></span>ipa-source</div>
    <form id="f" class="stack">
      <div><label>Benutzername</label><input name="username" required autocomplete="username" autofocus></div>
      <div><label>Passwort</label><input name="password" type="password" required autocomplete="current-password"></div>
      <button class="primary">Anmelden</button></form></div>`;
  $("#f").onsubmit = guard(async (e) => {
    e.preventDefault();
    await api("POST", "/auth/login", Object.fromEntries(new FormData(e.target)), { allow401: true });
    boot();
  });
}

// ----------------------------------------------------------------- apps --
async function renderApps() {
  const { apps } = await api("GET", "/apps");
  view.innerHTML = `<div class="head"><div><h1>Apps</h1><div class="muted">${apps.length} Apps im Feed</div></div>
    <div class="row"><a class="btn" href="#/upload">IPA hochladen</a><button id="syncall" class="primary">Alle Quellen synchronisieren</button></div></div>
    ${apps.length ? `<div class="grid">${apps.map((a) => `
      <a class="panel app-card" href="#/app/${a.id}">
        <img class="icon" src="${esc(a.icon || "/default-icon.png")}" alt="">
        <div style="min-width:0">
          <div class="title">${esc(a.name)} ${a.visible ? "" : '<span class="badge">versteckt</span>'} ${["missing", "blank"].includes(a.icon_status) ? iconBadge(a) : ""}</div>
          <div class="muted small mono" style="overflow:hidden;text-overflow:ellipsis">${esc(a.bundle_id)}</div>
          <div class="small">${a.latest ? `${esc(a.latest.version)} (${esc(a.latest.build_version)}) <span class="badge ${a.latest.origin}">${originLabel[a.latest.origin]}</span>` : "keine Builds"}
            <span class="muted">· ${a.build_count} Builds</span></div>
        </div></a>`).join("")}</div>`
      : `<div class="panel center muted" style="min-height:200px">Noch keine Apps. Lege eine <a href="#/sources">GitHub-Quelle</a> an oder <a href="#/upload">lade eine IPA hoch</a>.</div>`}`;
  $("#syncall").onclick = guard(async () => { await api("POST", "/sync"); toast("Sync gestartet"); });
}

async function renderApp(id) {
  const a = await api("GET", `/apps/${id}`);
  view.innerHTML = `
    <div class="head"><div class="row"><img class="icon lg" src="${esc(a.icon || "/default-icon.png")}">
      <div><h1>${esc(a.name)}</h1><div class="muted mono small">${esc(a.bundle_id)}</div><div style="margin-top:4px">${iconBadge(a)}</div></div></div>
      <div class="row"><a class="btn ${["missing", "blank"].includes(a.icon_status) ? "primary" : ""}" href="#/icon/${a.id}">Icon gestalten</a>
      <label class="btn">PNG nur für Feed<input type="file" accept="image/png" id="iconfile" hidden></label>
      <button class="danger" id="delapp">App löschen</button></div></div>

    ${a.icon_custom ? `<div class="panel row spread"><div><b>Eigenes Icon</b><div class="muted small">
        ${a.builds.filter((b) => b.icon_injected).length} von ${a.builds.length} Builds enthalten das Icon. Eingebaute Icons ersetzen das Icon der App auf dem Homescreen.</div></div>
      <div class="row"><label class="check"><input type="checkbox" id="autoinj" ${a.auto_inject ? "checked" : ""}> Neue Builds automatisch</label>
        <button id="injall" class="primary sm">In alle Builds einbauen</button><button id="rmicon" class="sm danger">Entfernen</button></div></div>` : ""}

    <div class="panel"><h2>Builds</h2>
      <table><thead><tr><th>Version</th><th>Herkunft</th><th>Datum</th><th>Größe</th><th>Icon</th><th>Im Feed</th><th>Fixiert</th><th></th></tr></thead><tbody>
      ${a.builds.map((b) => `<tr data-id="${b.id}">
        <td><b>${esc(b.version)}</b> <span class="muted">(${esc(b.build_version)})</span>${b.min_os ? `<div class="muted small">ab iOS ${esc(b.min_os)}</div>` : ""}</td>
        <td><span class="badge ${b.origin}">${originLabel[b.origin]}</span><div class="muted small">${esc(b.repo || "")} ${b.origin === "release" ? esc(b.origin_ref) : ""}</div></td>
        <td class="small">${fmtDate(b.date)}</td><td class="small">${fmtSize(b.size)}</td>
        <td class="small">${b.icon_state ? `<span class="badge ${b.icon_state.startsWith("Fehler") ? "err" : ""}" title="${esc(b.icon_state)}">${esc(b.icon_state.slice(0, 20))}</span>` : b.icon_injected ? '<span class="badge release">eingebaut</span>' : '<span class="muted">–</span>'}</td>
        <td><input type="checkbox" data-f="visible" ${b.visible ? "checked" : ""}></td>
        <td><input type="checkbox" data-f="pinned" ${b.pinned ? "checked" : ""} title="Wird von der Aufbewahrung nicht gelöscht"></td>
        <td class="row" style="justify-content:flex-end"><button class="sm primary" data-sign>Installieren</button><a class="btn sm" href="${esc(b.download_url)}">Laden</a>
          <button class="sm" data-notes>Notizen</button><button class="sm danger" data-del>Löschen</button></td></tr>`).join("")}
      </tbody></table></div>

    <form class="panel" id="meta"><h2>Store-Angaben</h2><div class="form">
      <div><label>Name</label><input name="name" value="${esc(a.name)}"></div>
      <div><label>Untertitel</label><input name="subtitle" value="${esc(a.subtitle)}"></div>
      <div><label>Entwickler</label><input name="developer" value="${esc(a.developer)}" placeholder="Standard aus Einstellungen"></div>
      <div><label>Kategorie</label><select name="category">${CATEGORIES.map((c) => `<option ${c === a.category ? "selected" : ""}>${c}</option>`).join("")}</select></div>
      <div><label>Farbe</label><input name="tint" value="${esc(a.tint)}" placeholder="#5C7AEA"></div>
      <div><label>Externe Icon-URL (optional)</label><input name="icon_url" value="${esc(a.icon_url)}"></div>
      <div class="full"><label>Beschreibung</label><textarea name="description">${esc(a.description)}</textarea></div>
      <div class="full"><label>Screenshot-URLs (eine pro Zeile)</label><textarea name="screenshots">${esc(a.screenshots.map((s) => (typeof s === "string" ? s : s.imageURL)).join("\n"))}</textarea></div>
      <div><label>Zusätzliche Entitlements (eine pro Zeile)</label><textarea name="entitlements" class="mono">${esc(a.entitlements.join("\n"))}</textarea></div>
      <div><label>Zusätzliche Privacy-Texte (JSON)</label><textarea name="privacy" class="mono">${esc(JSON.stringify(a.privacy, null, 1))}</textarea></div>
      <label class="check full"><input type="checkbox" name="visible" ${a.visible ? "checked" : ""}> Im Feed anzeigen</label>
      <div class="full"><button class="primary">Speichern</button></div></div></form>`;

  $("#meta").onsubmit = guard(async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    let privacy = {};
    try { privacy = JSON.parse(f.get("privacy") || "{}"); } catch { throw new Error("Privacy ist kein gültiges JSON"); }
    await api("PATCH", `/apps/${id}`, {
      name: f.get("name"), subtitle: f.get("subtitle"), developer: f.get("developer"), category: f.get("category"),
      tint: f.get("tint"), icon_url: f.get("icon_url"), description: f.get("description"), visible: f.get("visible") === "on",
      screenshots: f.get("screenshots").split("\n").map((s) => s.trim()).filter(Boolean),
      entitlements: f.get("entitlements").split("\n").map((s) => s.trim()).filter(Boolean), privacy,
    });
    toast("Gespeichert"); renderApp(id);
  });
  $("#autoinj")?.addEventListener("change", guard(async (e) => { await api("PATCH", `/apps/${id}`, { auto_inject: e.target.checked }); toast("Gespeichert"); }));
  $("#injall")?.addEventListener("click", guard(async () => { await api("POST", `/apps/${id}/inject-icon`, {}); toast("Icon wird eingebaut"); renderApp(id); }));
  $("#rmicon")?.addEventListener("click", guard(async () => {
    if (!confirm("Eigenes Icon entfernen? Bereits eingebaute Icons bleiben in den Builds.")) return;
    await api("DELETE", `/apps/${id}/custom-icon`); renderApp(id);
  }));
  if (a.builds.some((b) => ["wartet", "läuft"].includes(b.icon_state))) {
    clearInterval(timer);
    timer = setInterval(() => { if (location.hash === `#/app/${id}`) renderApp(id); else clearInterval(timer); }, 2500);
  }
  $("#iconfile").onchange = guard(async (e) => {
    await api("PUT", `/apps/${id}/icon`, e.target.files[0]); toast("Icon gespeichert"); renderApp(id);
  });
  $("#delapp").onclick = guard(async () => {
    if (!confirm(`„${a.name}“ mit allen Builds löschen?`)) return;
    await api("DELETE", `/apps/${id}`); location.hash = "#/apps";
  });
  $$("tr[data-id]").forEach((tr) => {
    const bid = tr.dataset.id, b = a.builds.find((x) => String(x.id) === bid);
    $$("input[data-f]", tr).forEach((cb) => cb.onchange = guard(async () => { await api("PATCH", `/builds/${bid}`, { [cb.dataset.f]: cb.checked }); toast("Gespeichert"); }));
    $("[data-del]", tr).onclick = guard(async () => { if (!confirm("Build löschen?")) return; await api("DELETE", `/builds/${bid}`); renderApp(id); });
    $("[data-sign]", tr).onclick = () => signFlow(b, a);
    $("[data-notes]", tr).onclick = () => modal(`<h2>Release-Notizen</h2><textarea name="notes" style="min-height:180px">${esc(b.notes)}</textarea>
      <div class="row" style="margin-top:12px"><button class="primary">Speichern</button><button value="cancel">Abbrechen</button></div>`,
      async (f) => { await api("PATCH", `/builds/${bid}`, { notes: f.get("notes") }); renderApp(id); });
  });
}

// -------------------------------------------------------------- sources --
function sourceForm(s = {}) {
  const v = (k, d) => esc(s[k] ?? d);
  const c = (k, d) => (s[k] ?? d) ? "checked" : "";
  const kind = s.kind || "github";
  return `<h2>${s.id ? "Quelle bearbeiten" : "Quelle hinzufügen"}</h2><div class="form">
    ${s.id ? `<div class="full"><label>${esc(s.provider || "GitHub")}</label><input value="${esc(s.web_url || s.repo)}" readonly></div>`
      : `<div><label>Anbieter</label><select name="kind" data-kind><option value="github" ${kind === "github" ? "selected" : ""}>GitHub</option><option value="gitea" ${kind === "gitea" ? "selected" : ""}>Gitea / Forgejo / Codeberg</option></select></div>
    <div data-gitea ${kind === "gitea" ? "" : "hidden"}><label>Server</label><input name="host" value="${v("host", "https://codeberg.org")}"></div>
    <div class="full"><label>Repository – owner/name oder einfach die URL einfügen</label><input name="repo" value="${v("repo", "")}" required placeholder="z. B. claration/Feather oder https://codeberg.org/owner/app"></div>`}
    <div class="full"><label>Anzeigename (optional)</label><input name="label" value="${v("label", "")}"></div>
    <label class="check"><input type="checkbox" name="use_releases" ${c("use_releases", true)}> Releases</label>
    <label class="check" data-gh ${kind === "gitea" ? "hidden" : ""}><input type="checkbox" name="use_artifacts" ${c("use_artifacts", false)}> Actions-Artefakte</label>
    <label class="check"><input type="checkbox" name="include_prereleases" ${c("include_prereleases", false)}> Pre-Releases</label>
    <label class="check"><input type="checkbox" name="enabled" ${c("enabled", true)}> Aktiv</label>
    <div><label>Dateimuster (Release / im Artefakt)</label><input name="asset_pattern" value="${v("asset_pattern", "*.ipa")}"></div>
    <div data-gh ${kind === "gitea" ? "hidden" : ""}><label>Artefakt-Name (Glob)</label><input name="artifact_name" value="${v("artifact_name", "*")}"></div>
    <div data-gh ${kind === "gitea" ? "hidden" : ""}><label>Nur Branch (leer = alle)</label><input name="artifact_branch" value="${v("artifact_branch", "")}"></div>
    <div><label>Releases behalten</label><input name="keep_releases" type="number" min="0" value="${v("keep_releases", s.id ? 5 : 3)}"></div>
    <div data-gh ${kind === "gitea" ? "hidden" : ""}><label>Artefakte behalten</label><input name="keep_artifacts" type="number" min="0" value="${v("keep_artifacts", 3)}"></div>
    <div class="full" data-gitea ${kind === "gitea" ? "" : "hidden"}><label>Zugangstoken (nur für private Repos)</label><input name="token" type="password" placeholder="${s.has_token ? "gespeichert – leer lassen zum Behalten" : "optional"}" autocomplete="off"></div>
  </div><div class="row" style="margin-top:16px"><button class="primary">Speichern</button><button value="cancel">Abbrechen</button></div>`;
}
function bindSourceForm(form) {
  const sel = form.querySelector("[data-kind]");
  const repo = form.querySelector("[name=repo]");
  const sync = () => {
    const g = sel.value === "gitea";
    form.querySelectorAll("[data-gitea]").forEach((e) => (e.hidden = !g));
    form.querySelectorAll("[data-gh]").forEach((e) => (e.hidden = g));
  };
  if (sel) {
    sel.onchange = sync;
    repo.addEventListener("input", () => {   // pasted URL decides the provider
      const m = repo.value.trim().match(/^(https?:\/\/)?([^/\s]+)\/[^/\s]+\/[^/\s]+/);
      if (!m || !m[2].includes(".")) return;
      const gh = /^(www\.)?github\.com$/i.test(m[2]);
      sel.value = gh ? "github" : "gitea";
      if (!gh) form.querySelector("[name=host]").value = (m[1] || "https://") + m[2];
      sync();
    });
  }
  return form;
}
const sourcePayload = (f) => ({
  ...(f.get("repo") !== null ? { repo: f.get("repo"), kind: f.get("kind"), host: f.get("host") } : {}),
  ...(f.get("token") ? { token: f.get("token") } : {}),
  label: f.get("label") || "",
  use_releases: f.get("use_releases") === "on", use_artifacts: f.get("use_artifacts") === "on",
  include_prereleases: f.get("include_prereleases") === "on", enabled: f.get("enabled") === "on",
  asset_pattern: f.get("asset_pattern"), artifact_name: f.get("artifact_name"), artifact_branch: f.get("artifact_branch"),
  keep_releases: +f.get("keep_releases"), keep_artifacts: +f.get("keep_artifacts"),
});

async function renderSources() {
  const [{ sources }, gh] = await Promise.all([api("GET", "/sources"), api("GET", "/github")]);
  view.innerHTML = `<div class="head"><div><h1>Quellen</h1>
      <div class="muted">${gh.connected ? `Verbunden als <b>${esc(gh.user || "?")}</b>` : `Nicht mit GitHub verbunden – <a href="#/settings">verbinden</a>`}</div></div>
    <div class="row"><a class="btn" href="#/github">Aus meinen GitHub-Repos wählen</a><button class="primary" id="add">Quelle hinzufügen</button></div></div>
    <div class="muted small" style="margin:-10px 0 16px">Auch fremde öffentliche Repos auf GitHub, Codeberg, Gitea oder Forgejo – neue Releases werden automatisch geladen. Ohne GitHub-Token erlaubt GitHub nur 60 Abfragen pro Stunde.</div>
    <div class="panel">${sources.length ? `<table><thead><tr><th>Repository</th><th>Holt</th><th>Builds</th><th>Letzter Sync</th><th></th></tr></thead><tbody>
      ${sources.map((s) => `<tr data-id="${s.id}">
        <td><a href="${esc(s.web_url)}" target="_blank" rel="noopener"><b>${esc(s.label || s.repo)}</b></a> <span class="badge">${esc(s.provider)}</span> ${s.enabled ? "" : '<span class="badge">inaktiv</span>'}${s.syncing ? ' <span class="badge">läuft …</span>' : ""}${s.label ? `<div class="muted small">${esc(s.repo)}</div>` : ""}${s.kind === "gitea" && !s.host.includes("codeberg.org") ? `<div class="muted small">${esc(s.host)}</div>` : ""}</td>
        <td class="small">${[s.use_releases && "Releases", s.use_artifacts && `Artefakte${s.artifact_branch ? ` (${esc(s.artifact_branch)})` : ""}`].filter(Boolean).join(", ")}</td>
        <td>${s.build_count}</td>
        <td class="small">${fmtDate(s.last_sync_at)}<div class="muted">${esc(s.last_status)}</div>${s.last_error ? `<div class="badge err" title="${esc(s.last_error)}">Fehler</div>` : ""}</td>
        <td class="row" style="justify-content:flex-end"><button class="sm" data-sync>Sync</button><button class="sm" data-edit>Bearbeiten</button><button class="sm danger" data-del>Entfernen</button></td></tr>`).join("")}
      </tbody></table>` : `<div class="muted">Noch keine Quellen.</div>`}</div>`;
  $("#add").onclick = () => bindSourceForm(modal(sourceForm(), async (f) => { await api("POST", "/sources", sourcePayload(f)); toast("Quelle angelegt, Sync läuft"); renderSources(); }));
  $$("tr[data-id]").forEach((tr) => {
    const s = sources.find((x) => String(x.id) === tr.dataset.id);
    $("[data-sync]", tr).onclick = guard(async () => { await api("POST", `/sources/${s.id}/sync`); toast("Sync gestartet"); setTimeout(renderSources, 1500); });
    $("[data-edit]", tr).onclick = () => modal(sourceForm(s), async (f) => { await api("PATCH", `/sources/${s.id}`, sourcePayload(f)); renderSources(); });
    $("[data-del]", tr).onclick = () => modal(`<h2>${esc(s.repo)} entfernen?</h2>
      <label class="check"><input type="checkbox" name="builds"> Auch die ${s.build_count} geladenen Builds löschen</label>
      <div class="row" style="margin-top:16px"><button class="primary">Entfernen</button><button value="cancel">Abbrechen</button></div>`,
      async (f) => { await api("DELETE", `/sources/${s.id}?delete_builds=${f.get("builds") === "on" ? 1 : 0}`); renderSources(); });
  });
  if (sources.some((s) => s.syncing)) timer = setInterval(() => location.hash.startsWith("#/sources") && renderSources(), 4000);
}

async function renderGithubRepos() {
  view.innerHTML = `<div class="head"><div><h1>Meine GitHub-Repos</h1><div class="muted">Repos scannen und als Quelle übernehmen</div></div>
    <div class="row"><input id="filter" placeholder="Filtern …" style="width:200px"><button id="scanall">Alle sichtbaren scannen</button></div></div>
    <div class="panel" id="list"><div class="muted">Lade Repos …</div></div>`;
  const { repos } = await api("GET", "/github/repos");
  const scans = {};
  const draw = () => {
    const q = $("#filter").value.toLowerCase();
    const shown = repos.filter((r) => !r.archived && r.full_name.toLowerCase().includes(q));
    $("#list").innerHTML = `<table><thead><tr><th>Repository</th><th>Fund</th><th></th></tr></thead><tbody>${shown.map((r) => {
      const s = scans[r.full_name];
      const found = !s ? '<span class="muted small">nicht gescannt</span>' : s === "…" ? '<span class="muted small">scanne …</span>'
        : s.error && !s.suggested ? `<span class="badge err" title="${esc(s.error)}">Fehler</span>`
        : `${s.releases_with_ipa ? `<span class="badge release">${s.releases_with_ipa} Releases mit IPA</span> ` : ""}${s.artifact_names.map((a) => `<span class="badge artifact">${esc(a.name)} ×${a.count}</span>`).join(" ")}${!s.suggested ? '<span class="muted small">keine IPAs gefunden</span>' : ""}`;
      return `<tr data-repo="${esc(r.full_name)}"><td><b>${esc(r.full_name)}</b> ${r.private ? '<span class="badge">privat</span>' : ""}<div class="muted small">${esc(r.description)}</div></td>
        <td>${found}</td><td class="row" style="justify-content:flex-end">${r.source_id ? '<span class="badge release">Quelle</span>'
          : `<button class="sm" data-scan>Scannen</button><button class="sm ${s?.suggested ? "primary" : ""}" data-add>Hinzufügen</button>`}</td></tr>`;
    }).join("")}</tbody></table>`;
    $$("tr[data-repo]").forEach((tr) => {
      const name = tr.dataset.repo;
      $("[data-scan]", tr)?.addEventListener("click", () => scan(name));
      $("[data-add]", tr)?.addEventListener("click", () => {
        const s = scans[name] && scans[name] !== "…" ? scans[name] : null;
        const art = s?.artifact_names?.find((a) => /ipa|ios/i.test(a.name)) || s?.artifact_names?.[0];
        modal(sourceForm({ repo: name, use_releases: !s || s.releases_with_ipa > 0, use_artifacts: !!art, artifact_name: art ? art.name : "*" }),
          async (f) => { await api("POST", "/sources", sourcePayload(f)); toast("Quelle angelegt, Sync läuft"); repos.find((r) => r.full_name === name).source_id = 1; draw(); });
      });
    });
  };
  const scan = async (name) => {
    scans[name] = "…"; draw();
    const [o, r] = name.split("/");
    try { scans[name] = await api("GET", `/github/repos/${o}/${r}/scan`); } catch (e) { scans[name] = { error: e.message, artifact_names: [] }; }
    draw();
  };
  $("#filter").oninput = draw;
  $("#scanall").onclick = async () => {
    const q = $("#filter").value.toLowerCase();
    const todo = repos.filter((r) => !r.archived && !r.source_id && r.full_name.toLowerCase().includes(q) && !scans[r.full_name]).map((r) => r.full_name);
    const workers = Array.from({ length: 4 }, async () => { while (todo.length) await scan(todo.shift()); });
    await Promise.all(workers);
  };
  draw();
}

// ------------------------------------------------- signing & installing --
const isIOS = /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
const typeLabel = { development: "Development", adhoc: "Ad Hoc", enterprise: "Enterprise", appstore: "App Store", distribution: "Distribution" };

async function signFlow(build, app) {
  const { certs: list, capabilities: caps } = await api("GET", "/certs");
  if (!caps.zsign) return toast("zsign ist auf dem Server nicht installiert – siehe README", true);
  if (!list.length) {
    if (confirm("Noch kein Signier-Zertifikat hinterlegt. Jetzt hinzufügen?")) location.hash = "#/signing";
    return;
  }
  const form = modal(`<h2>${esc(app.name)} ${esc(build.version)} (${esc(build.build_version)})</h2>
    <p class="muted small">Der Server signiert die IPA mit deinem Zertifikat. Danach installierst du sie direkt per Link auf dem iPhone oder per USB-Kabel am Server.</p>
    ${list.length > 1 ? `<div><label>Zertifikat</label><select name="cert">${list.map((c) => `<option value="${c.id}" ${c.is_default ? "selected" : ""}>${esc(c.name)} – ${typeLabel[c.profile_type] || c.profile_type}${c.expired ? " (abgelaufen)" : ""}</option>`).join("")}</select></div>` : `<div class="muted small">Zertifikat: <b>${esc(list[0].name)}</b></div>`}
    ${(caps.fixes || []).map((f) => `<label class="check" style="margin-top:12px" title="${esc(f.description)}">
      <input type="checkbox" name="fix_${f.key}" ${f.available ? "" : "disabled"} ${f.available && (app.sign_options || {})[f.key] ? "checked" : ""}>
      ${esc(f.label)}${f.available ? "" : ' <span class="muted small">(im Image noch nicht enthalten)</span>'}</label>
      <div class="muted small" style="margin-left:26px">${esc(f.description)}</div>`).join("")}
    <details style="margin-top:10px"><summary class="small">Erweitert</summary>
      <div style="margin-top:8px"><label>Bundle-ID (leer = unverändert ${esc(app.bundle_id)})</label>
      <input name="bundle_id" placeholder="z. B. ${esc(app.bundle_id)}.zweit" autocomplete="off" autocapitalize="off">
      <div class="muted small">Eine andere Bundle-ID installiert die App zusätzlich statt sie zu ersetzen.</div></div></details>
    <div id="signstate" class="stack" style="margin-top:14px"></div>
    <div class="row" style="margin-top:16px"><button class="primary" id="dosign">Signieren</button><button value="cancel">Schließen</button></div>`);
  const stateEl = $("#signstate", form);
  const poll = async (sid) => {
    for (;;) {
      const s = await api("GET", `/signed/${sid}`);
      if (s.status === "fertig") { stateEl.innerHTML = await signedActions(s); bindSignedActions(stateEl, s); $("#dosign", form).hidden = true; return; }
      if (s.status === "fehler") {
        const d = await api("GET", `/signed/${sid}?log=1`);
        stateEl.innerHTML = `<div><span class="badge err">Fehler</span> ${esc(s.error)}</div>
          <details><summary class="small">Log anzeigen</summary><pre class="mono small" style="max-height:40vh;overflow:auto;white-space:pre-wrap">${esc(d.log || "(leer)")}</pre></details>`;
        $("#dosign", form).disabled = false; return;
      }
      stateEl.innerHTML = `<div class="muted">${s.status === "wartet" ? "wartet …" : "wird signiert … (große Apps brauchen etwa 1 Minute pro 10 GB)"}</div><progress></progress>`;
      if (!$("#modal").open) return;
      await sleep(1500);
    }
  };
  $("#dosign", form).onclick = guard(async (e) => {
    e.preventDefault();
    $("#dosign", form).disabled = true;
    const cert = form.querySelector("[name=cert]")?.value;
    const bid = form.querySelector("[name=bundle_id]")?.value.trim();
    const options = Object.fromEntries((caps.fixes || []).map((f) => [f.key, !!form.querySelector(`[name=fix_${f.key}]`)?.checked]));
    const s = await api("POST", `/builds/${build.id}/sign`, { ...(cert ? { cert_id: +cert } : {}), ...(bid ? { bundle_id: bid } : {}), options });
    app.sign_options = options;
    poll(s.id);
  });
}

async function signedActions(s) {
  let devs = { devices: [] };
  try { devs = await api("GET", "/devices"); } catch { /* */ }
  const usable = devs.devices.filter((d) => d.paired);
  return `<div><span class="badge release">signiert</span>${(s.options || {}).filepicker ? ' <span class="badge upload">File Picker Fix</span>' : ""} <span class="muted small">${fmtSize(s.size)} · ${esc(s.cert_name)}${s.outdated ? ' · <b>veraltet</b> (Build wurde geändert)' : ""}</span></div>
    ${s.ota_ready ? (isIOS
      ? `<a class="btn primary" href="${esc(s.itms_url)}" style="justify-content:center">Auf diesem Gerät installieren</a>`
      : `<div><label>Installationslink fürs iPhone (in Safari öffnen)</label>${copyBox(s.install_page)}</div>`)
      : '<div class="small"><span class="badge err">kein HTTPS</span> Für die Installation per Link braucht der Server eine https://-Adresse (BASE_URL).</div>'}
    ${usable.length ? `<div class="row"><select data-udid style="flex:1">${usable.map((d) => `<option value="${esc(d.udid)}">${esc(d.name || d.udid)} (iOS ${esc(d.ios)})</option>`).join("")}</select><button class="sm" data-usb>Per USB installieren</button></div><div data-usbstate></div>` : ""}
    <div class="row"><a class="btn sm" href="${esc(s.download_url)}">Signierte IPA laden</a></div>`;
}

function bindSignedActions(el, s) {
  const btn = $("[data-usb]", el);
  if (!btn) return;
  btn.onclick = guard(async (e) => {
    e.preventDefault();
    btn.disabled = true;
    const st = $("[data-usbstate]", el);
    let j = await api("POST", `/signed/${s.id}/install-usb`, { udid: $("[data-udid]", el).value });
    while (j.status === "läuft") {
      st.innerHTML = `<progress max="100" value="${j.progress || 0}"></progress><div class="small muted">${esc(j.message)}</div>`;
      await sleep(1500);
      j = await api("GET", `/jobs/${j.id}`);
    }
    st.innerHTML = j.status === "fertig" ? '<div>✓ Installiert</div>'
      : `<div><span class="badge err">Fehler</span> ${esc(j.message)}</div>
         <details><summary class="small">Log anzeigen</summary><pre class="mono small" style="max-height:40vh;overflow:auto;white-space:pre-wrap">${esc(j.log || "(leer)")}</pre></details>`;
    btn.disabled = false;
  });
}

async function renderSigning() {
  const [{ certs: list, capabilities: caps }, { signed }] = await Promise.all([api("GET", "/certs"), api("GET", "/signed")]);
  let devs = { available: false, devices: [] };
  try { devs = await api("GET", "/devices"); } catch { /* */ }
  const now = new Date().toISOString();
  view.innerHTML = `<div class="head"><div><h1>Signieren &amp; Installieren</h1>
      <div class="muted">Der Server signiert IPAs selbst (zsign) und installiert sie per Link oder USB – ohne Signier-App auf dem iPhone.</div></div></div>
    ${!caps.zsign ? '<div class="panel"><span class="badge err">zsign fehlt</span> Das Docker-Image ab Version 0.4 enthält zsign. Bitte Image aktualisieren.</div>' : ""}

    <div class="panel"><div class="row spread"><h2>Zertifikate</h2><button class="primary sm" id="addcert">Zertifikat hinzufügen</button></div>
      ${list.length ? `<table><thead><tr><th>Name</th><th>Typ</th><th>Gültig bis</th><th>Geräte</th><th></th></tr></thead><tbody>${list.map((c) => `<tr data-id="${c.id}">
        <td><b>${esc(c.name)}</b> ${c.is_default ? '<span class="badge release">Standard</span>' : ""}<div class="muted small">${esc(c.cert_cn)}<br>Team ${esc(c.team_name || c.team_id)} · App-ID <span class="mono">${esc(c.app_id)}</span></div></td>
        <td class="small">${typeLabel[c.profile_type] || esc(c.profile_type)}</td>
        <td class="small">Zert.: ${fmtDate(c.cert_expires)}<br>Profil: ${fmtDate(c.profile_expires)} ${c.expired ? '<span class="badge err">abgelaufen</span>' : ""}</td>
        <td class="small">${c.profile_type === "enterprise" ? "alle" : c.devices.length}</td>
        <td class="row" style="justify-content:flex-end">${c.wildcard ? "" : `<label class="check small" title="Nur nötig, wenn Apps mit Original-Bundle-ID nicht installieren. Dann ersetzen sich alle Apps dieses Profils gegenseitig."><input type="checkbox" data-adapt ${c.adapt_bundle_id ? "checked" : ""}> Bundle-ID an Profil anpassen</label>`}
          ${c.is_default ? "" : '<button class="sm" data-def>Als Standard</button>'}<button class="sm danger" data-del>Löschen</button></td></tr>`).join("")}</tbody></table>`
        : '<div class="muted">Noch kein Zertifikat. Du brauchst die .p12-Datei, ihr Passwort und das passende Provisioning-Profil (.mobileprovision).</div>'}
      <div class="muted small" style="margin-top:10px">Das Passwort wird verschlüsselt gespeichert; die Dateien liegen nur auf deinem Server (<code>/data/certs</code>).</div></div>

    <div class="panel"><div class="row spread"><h2>Geräte per USB</h2><button class="sm" id="refreshdev">Aktualisieren</button></div>
      ${!devs.available ? '<div class="muted small">libimobiledevice ist nicht verfügbar. Für USB-Installation das iPhone an den Docker-Host anschließen und den usbmuxd-Socket in den Container einbinden (siehe README).</div>'
        : devs.devices.length ? `<table><tbody>${devs.devices.map((d) => `<tr data-udid="${esc(d.udid)}"><td><b>${esc(d.name || "Unbekanntes Gerät")}</b><div class="muted small mono">${esc(d.udid)}</div></td>
            <td class="small">${d.ios ? "iOS " + esc(d.ios) : ""} ${esc(d.model)}</td><td><span class="badge">${d.connection === "usb" ? "USB" : "WLAN"}</span></td>
            <td style="text-align:right">${d.paired ? '<span class="badge release">gekoppelt</span>' : '<button class="sm primary" data-pair>Koppeln</button>'}</td></tr>`).join("")}</tbody></table>`
        : `<div class="muted small">Kein iPhone gefunden. Per USB anschließen und entsperren.${devs.usbmuxd ? (devs.usbmuxd_remote ? ` (verbunden mit ${esc(devs.usbmuxd_remote)})` : "") : " (usbmuxd nicht eingebunden – Socket mounten oder USBMUXD_SOCKET_ADDRESS setzen)"}</div>`}</div>

    <div class="panel"><h2>Signierte IPAs</h2>
      ${signed.length ? `<table><thead><tr><th>App</th><th>Zertifikat</th><th>Status</th><th></th></tr></thead><tbody>${signed.map((x) => `<tr data-sid="${x.id}">
        <td><a href="#/app/${x.app_id}"><b>${esc(x.app_name)}</b></a> ${esc(x.version)} (${esc(x.build_version)})<div class="muted small mono">${esc(x.bundle_id)}${(x.options || {}).filepicker ? ' <span class="badge upload">File Picker Fix</span>' : ""}</div><div class="muted small">${fmtDate(x.finished_at || x.created_at)}${x.size ? " · " + fmtSize(x.size) : ""}</div></td>
        <td class="small">${esc(x.cert_name)}</td>
        <td>${x.status === "fertig" ? `<span class="badge release">bereit</span>${x.outdated ? ' <span class="badge">veraltet</span>' : ""}` : x.status === "fehler" ? `<span class="badge err" title="${esc(x.error)}">Fehler</span>` : `<span class="badge">${esc(x.status)}</span>`}</td>
        <td class="row" style="justify-content:flex-end">${x.status === "fertig" ? '<button class="sm primary" data-inst>Installieren</button>' : ""}${x.status === "fehler" ? '<button class="sm" data-log>Log</button>' : ""}<button class="sm danger" data-sdel>Löschen</button></td></tr>`).join("")}</tbody></table>`
        : '<div class="muted small">Noch nichts signiert. Auf der App-Seite bei einem Build auf „Installieren“ tippen.</div>'}
      <div class="muted small" style="margin-top:10px">Pro App und Zertifikat wird nur die neueste signierte Kopie aufbewahrt.</div></div>`;

  $("#addcert").onclick = () => modal(`<h2>Signier-Zertifikat hinzufügen</h2><div class="form">
      <div class="full"><label>Name (optional)</label><input name="name" placeholder="z. B. Developer Marc"></div>
      <div class="full"><label>Zertifikat (.p12)</label><input type="file" name="p12" accept=".p12,.pfx,application/x-pkcs12" required></div>
      <div class="full"><label>Passwort der .p12</label><input type="password" name="password" autocomplete="off"></div>
      <div class="full"><label>Provisioning-Profil (.mobileprovision)</label><input type="file" name="profile" accept=".mobileprovision" required></div></div>
      <div class="row" style="margin-top:16px"><button class="primary">Prüfen &amp; speichern</button><button value="cancel">Abbrechen</button></div>`,
    async (f) => { await api("POST", "/certs", f); toast("Zertifikat gespeichert"); renderSigning(); });
  $("#refreshdev").onclick = guard(renderSigning);
  $$("tr[data-id]").forEach((tr) => {
    const id = tr.dataset.id;
    $("[data-def]", tr)?.addEventListener("click", guard(async () => { await api("PATCH", `/certs/${id}`, { is_default: true }); renderSigning(); }));
    $("[data-adapt]", tr)?.addEventListener("change", guard(async (e) => { await api("PATCH", `/certs/${id}`, { adapt_bundle_id: e.target.checked }); toast("Gespeichert – gilt fürs nächste Signieren"); }));
    $("[data-del]", tr).onclick = guard(async () => { if (!confirm("Zertifikat und alle damit signierten IPAs löschen?")) return; await api("DELETE", `/certs/${id}`); renderSigning(); });
  });
  $$("tr[data-udid]").forEach((tr) => {
    $("[data-pair]", tr)?.addEventListener("click", guard(async () => {
      toast("Bitte auf dem iPhone „Vertrauen“ antippen …");
      const r = await fetch(`/api/v1/devices/${encodeURIComponent(tr.dataset.udid)}/pair`, { method: "POST", headers: { "X-Requested-With": "ipa-source" } });
      const d = await r.json(); toast(d.message || d?.error?.message, !d.ok); renderSigning();
    }));
  });
  $$("tr[data-sid]").forEach((tr) => {
    const x = signed.find((y) => String(y.id) === tr.dataset.sid);
    $("[data-inst]", tr)?.addEventListener("click", guard(async () => {
      const form = modal(`<h2>${esc(x.app_name)} ${esc(x.version)} installieren</h2><div id="sa" class="stack"></div><div class="row" style="margin-top:16px"><button value="cancel">Schließen</button></div>`);
      const el = $("#sa", form); el.innerHTML = await signedActions(x); bindSignedActions(el, x);
    }));
    $("[data-log]", tr)?.addEventListener("click", guard(async () => {
      const d = await api("GET", `/signed/${x.id}?log=1`);
      modal(`<h2>zsign-Log</h2><pre class="mono small" style="max-height:50vh;overflow:auto;white-space:pre-wrap">${esc(d.log || d.error)}</pre><div class="row"><button value="cancel">Schließen</button></div>`);
    }));
    $("[data-sdel]", tr).onclick = guard(async () => { await api("DELETE", `/signed/${x.id}`); renderSigning(); });
  });
}

// -------------------------------------------------------------- downloads --
const dlStatus = { wartet: "wartet", läuft: "lädt", auswahl: "Auswahl nötig", fertig: "fertig", fehler: "Fehler", abgebrochen: "abgebrochen" };

async function renderDownloads() {
  const [{ downloads }, s] = await Promise.all([api("GET", "/downloads"), api("GET", "/settings")]);
  const endpoint = `${s.base_url_effective}/api/v1/downloads`;
  view.innerHTML = `<div class="head"><div><h1>Downloads</h1><div class="muted">IPA-Links laden – direkt, von Webseiten, Release-Seiten oder itms-services-Links.</div></div></div>
    <form class="panel row" id="dlform"><input name="url" placeholder="https://… (IPA-Link, Webseite oder Repo-URL)" style="flex:1;min-width:260px" required>
      <input name="notes" placeholder="Notiz (optional)" style="width:200px"><button class="primary">Laden</button></form>
    <div class="panel">${downloads.length ? `<table><tbody>${downloads.map((d) => `<tr data-id="${d.id}">
      <td style="max-width:420px"><div class="mono small" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(d.url)}">${esc(d.url)}</div>
        <div class="small muted">${fmtDate(d.created_at)}${d.via === "shortcut" ? " · per Kurzbefehl" : ""}${d.notes ? " · " + esc(d.notes) : ""}</div>
        ${d.status === "läuft" ? `<progress max="${d.total || 1}" value="${d.received}"></progress><div class="small muted">${d.received ? fmtSize(d.received) + (d.total ? " von " + fmtSize(d.total) : "") : "verbinde …"}</div>` : ""}
        ${d.status === "fehler" ? `<div class="small"><span class="badge err">Fehler</span> ${esc(d.error)}</div>` : ""}
        ${d.status === "auswahl" ? `<div class="small" style="margin-top:6px">Mehrere IPAs gefunden – bitte wählen:</div>${d.candidates.map((c, i) => `<div><button class="sm" data-pick="${i}">${esc(c.label)}${c.size ? " · " + fmtSize(c.size) : ""}</button></div>`).join("")}` : ""}
        ${d.status === "fertig" && d.app ? `<div class="small">✓ <a href="#/app/${d.app.id}">${esc(d.app.name)}</a> ${esc(d.build?.version || "")} (${esc(d.build?.build_version || "")})</div>` : ""}
        ${d.repo_hint && !d.repo_watched ? `<div style="margin-top:6px"><button class="sm" data-watch>Repo ${esc(d.repo_hint.repo)} überwachen</button></div>` : ""}</td>
      <td><span class="badge ${d.status === "fertig" ? "release" : d.status === "fehler" ? "err" : ""}">${dlStatus[d.status] || esc(d.status)}</span></td>
      <td class="row" style="justify-content:flex-end">${["fehler", "abgebrochen"].includes(d.status) ? '<button class="sm" data-retry>Erneut</button>' : ""}
        <button class="sm danger" data-del>${["wartet", "läuft"].includes(d.status) ? "Abbrechen" : "Entfernen"}</button></td></tr>`).join("")}</tbody></table>`
      : '<div class="muted">Noch keine Downloads.</div>'}</div>

    <div class="panel stack"><h2>iOS-Kurzbefehl „An ipa-source senden“</h2>
      <div class="muted small">Damit schickst du Links direkt aus Safari (oder jeder App mit Teilen-Menü) an deinen Server. Signierte Kurzbefehl-Dateien kann nur Apple erzeugen – deshalb baust du ihn einmal selbst (2 Minuten), danach kannst du ihn per iCloud-Link teilen.</div>
      <div><label>1. Token für den Kurzbefehl (darf nur hochladen/laden)</label>
        <div id="sctoken"><button class="sm primary" id="mktoken">Token erstellen</button></div></div>
      <div><label>2. Adresse</label>${copyBox(endpoint)}</div>
      <ol class="small" style="line-height:1.7;margin:0;padding-left:20px">
        <li>App <b>Kurzbefehle</b> → <b>+</b> → oben auf den Namen tippen → „An ipa-source senden“.</li>
        <li>Unten auf <b>(i)</b>: <b>Im Share-Sheet anzeigen</b> einschalten. Bei „Share-Sheet-Typen“ nur <b>URLs</b> und <b>Safari-Webseiten</b> lassen.
          „Wenn keine Eingabe“: <b>Zwischenablage abrufen</b> (dann funktioniert er auch per Siri/Home-Bildschirm mit kopiertem Link).</li>
        <li>Aktion <b>Inhalte von URL abrufen</b> hinzufügen. URL = die Adresse oben. Auf „Mehr anzeigen“:
          Methode <b>POST</b> · Header: <code>Authorization</code> = <code>Bearer &lt;Token&gt;</code> ·
          Anfragetext <b>JSON</b> → Feld hinzufügen: Typ <b>Text</b>, Schlüssel <code>url</code>, Wert: Variable <b>Kurzbefehleingabe</b>.</li>
        <li>Aktion <b>Wörterbuchwert abrufen</b>: Schlüssel <code>message</code> aus <b>Inhalte von URL</b>.</li>
        <li>Aktion <b>Mitteilung anzeigen</b> mit dem <b>Wörterbuchwert</b>.</li>
      </ol>
      <div class="muted small">Hinter Pangolin: den Pfad <code>/api/v1/downloads</code> ohne Login freigeben – er ist durch das Token geschützt.</div>
    </div>`;

  $("#dlform").onsubmit = guard(async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    await api("POST", "/downloads", { url: f.get("url"), notes: f.get("notes") });
    e.target.reset(); renderDownloads();
  });
  $("#mktoken").onclick = guard(async () => {
    const r = await api("POST", "/tokens", { name: "iOS Kurzbefehl", scope: "upload" });
    $("#sctoken").innerHTML = `${copyBox("Bearer " + r.token)}<div class="muted small">Nur jetzt sichtbar – komplett (inkl. „Bearer “) als Header-Wert eintragen.</div>`;
  });
  $$("tr[data-id]").forEach((tr) => {
    const d = downloads.find((x) => String(x.id) === tr.dataset.id);
    $$("[data-pick]", tr).forEach((b) => b.onclick = guard(async () => { await api("POST", `/downloads/${d.id}/choose`, { url: d.candidates[+b.dataset.pick].url }); renderDownloads(); }));
    $("[data-retry]", tr)?.addEventListener("click", guard(async () => { await api("POST", `/downloads/${d.id}/retry`); renderDownloads(); }));
    $("[data-del]", tr).onclick = guard(async () => { await api("DELETE", `/downloads/${d.id}`); renderDownloads(); });
    $("[data-watch]", tr)?.addEventListener("click", () => bindSourceForm(modal(sourceForm({ kind: d.repo_hint.kind, host: d.repo_hint.host, repo: d.repo_hint.repo, keep_releases: 2 }),
      async (f) => { await api("POST", "/sources", sourcePayload(f)); toast("Repo wird überwacht"); renderDownloads(); })));
  });
  if (downloads.some((d) => ["wartet", "läuft"].includes(d.status))) {
    clearInterval(timer);
    timer = setInterval(() => {
      if (!location.hash.startsWith("#/downloads")) return clearInterval(timer);
      const busy = $("#sctoken input") || document.activeElement?.closest("#dlform") || $("#modal").open;
      if (!busy) renderDownloads();
    }, 2000);
  }
}

// --------------------------------------------------------- icon designer --
const ICON_SIZES = [1024, 180, 167, 152, 120, 76];

async function renderIconDesigner(id) {
  const a = await api("GET", `/apps/${id}`);
  const st = { img: null, zoom: 1, x: 0, y: 0, bg1: "#ffffff", bg2: "#5c7aea", grad: false, angle: 135,
               text: "", tsize: 300, tcolor: "#ffffff", ty: 50, tweight: "700", shadow: false };
  view.innerHTML = `<div class="head"><div><h1>Icon gestalten</h1><div class="muted"><a href="#/app/${a.id}">${esc(a.name)}</a> · ${esc(a.bundle_id)}</div></div></div>
  <div class="designer">
    <div class="panel stack" style="text-align:center">
      <canvas id="cv" width="1024" height="1024" class="icon-canvas"></canvas>
      <div class="muted small">Bild mit der Maus/dem Finger verschieben · Mausrad zoomt</div>
      <div class="row" style="justify-content:center;gap:24px">
        <div class="home light"><canvas width="120" height="120" class="mini"></canvas><span>${esc(a.name)}</span></div>
        <div class="home dark"><canvas width="120" height="120" class="mini"></canvas><span>${esc(a.name)}</span></div>
      </div>
    </div>
    <div class="stack">
      <div class="panel stack"><h2>Bild</h2>
        <div class="row"><label class="btn sm">Datei wählen<input type="file" accept="image/*" id="dfile" hidden></label>
          <button class="sm" id="dcurrent">Aktuelles Icon</button><button class="sm" id="dclear">Kein Bild</button></div>
        <div class="copy"><input id="durl" placeholder="Bild-URL aus dem Internet (PNG, JPG, SVG, WebP)"><button class="sm" id="dload">Laden</button></div>
        <div class="muted small">Tipp: Bild kopieren und hier mit Strg/⌘+V einfügen.</div>
        <div><label>Größe <span id="zv"></span></label><input type="range" id="zoom" min="5" max="400" value="100"></div>
        <div class="row"><button class="sm" id="dcenter">Zentrieren</button><button class="sm" id="dfill">Füllend</button><button class="sm" id="dfit">Einpassen</button></div>
      </div>
      <div class="panel stack"><h2>Hintergrund</h2>
        <div class="row"><input type="color" id="bg1" value="#ffffff"><label class="check"><input type="checkbox" id="grad"> Verlauf</label>
          <input type="color" id="bg2" value="#5c7aea"><input type="range" id="angle" min="0" max="360" value="135" style="flex:1"></div>
        <div class="row">${["#ffffff", "#000000", "#1c1c1e", "#5c7aea", "#ff3b30", "#ff9500", "#34c759", "#007aff", "#af52de"].map((c) => `<button class="swatch" data-c="${c}" style="background:${c}" title="${c}"></button>`).join("")}</div>
      </div>
      <div class="panel stack"><h2>Text / Emoji</h2>
        <input id="dtext" placeholder="z. B. R5 oder 📡" maxlength="12">
        <div class="row"><input type="color" id="tcolor" value="#ffffff"><select id="tweight" style="width:auto"><option value="400">Normal</option><option value="700" selected>Fett</option><option value="900">Schwarz</option></select>
          <label class="check"><input type="checkbox" id="tshadow"> Schatten</label></div>
        <div><label>Textgröße</label><input type="range" id="tsize" min="60" max="800" value="300"></div>
        <div><label>Vertikale Position</label><input type="range" id="ty" min="10" max="90" value="50"></div>
      </div>
      <div class="panel stack"><h2>Speichern</h2>
        <label class="check"><input type="checkbox" id="sinject" checked> In alle vorhandenen Builds einbauen (Homescreen-Icon)</label>
        <label class="check"><input type="checkbox" id="sauto" ${a.auto_inject || !a.icon_custom ? "checked" : ""}> Auch in künftige Builds automatisch einbauen</label>
        <div class="row"><button class="primary" id="dsave">Speichern</button><a class="btn" href="#/app/${a.id}">Abbrechen</a>
          <button class="sm link" id="dpng">Als PNG herunterladen</button></div>
      </div>
    </div>
  </div>`;

  const cv = $("#cv"), ctx = cv.getContext("2d");
  const minis = $$(".mini");

  function base() { // scale at which the image fits ("contain") in 80 % of the canvas
    if (!st.img) return 1;
    return Math.min(1024 * 0.8 / st.img.width, 1024 * 0.8 / st.img.height);
  }
  function paint(c, size) {
    const k = size / 1024;
    c.save();
    c.clearRect(0, 0, size, size);
    if (st.grad) {
      const r = (st.angle - 90) * Math.PI / 180, cx = size / 2, d = size / 2;
      const g = c.createLinearGradient(cx - Math.cos(r) * d, cx - Math.sin(r) * d, cx + Math.cos(r) * d, cx + Math.sin(r) * d);
      g.addColorStop(0, st.bg1); g.addColorStop(1, st.bg2); c.fillStyle = g;
    } else c.fillStyle = st.bg1;
    c.fillRect(0, 0, size, size);
    c.imageSmoothingEnabled = true; c.imageSmoothingQuality = "high";
    if (st.img) {
      const s = base() * st.zoom * k, w = st.img.width * s, h = st.img.height * s;
      c.drawImage(st.img, size / 2 - w / 2 + st.x * k, size / 2 - h / 2 + st.y * k, w, h);
    }
    if (st.text) {
      c.font = `${st.tweight} ${st.tsize * k}px -apple-system, "SF Pro Rounded", "Segoe UI", system-ui, sans-serif`;
      c.textAlign = "center"; c.textBaseline = "middle"; c.fillStyle = st.tcolor;
      if (st.shadow) { c.shadowColor = "rgba(0,0,0,.35)"; c.shadowBlur = 24 * k; c.shadowOffsetY = 8 * k; }
      c.fillText(st.text, size / 2, size * st.ty / 100);
    }
    c.restore();
  }
  function draw() {
    paint(ctx, 1024);
    minis.forEach((m) => paint(m.getContext("2d"), 120));
    $("#zv").textContent = `${Math.round(st.zoom * 100)} %`;
  }
  async function loadSrc(src) {
    const img = new Image();
    img.decoding = "async";
    await new Promise((ok, bad) => { img.onload = ok; img.onerror = () => bad(new Error("Bild konnte nicht geöffnet werden")); img.src = src; });
    if (!img.width) { img.width = 1024; img.height = 1024; }  // SVG without intrinsic size
    st.img = img; st.zoom = 1; st.x = 0; st.y = 0; $("#zoom").value = 100;
    draw();
  }
  const loadBlob = (blob) => loadSrc(URL.createObjectURL(blob));

  $("#dfile").onchange = guard(async (e) => { if (e.target.files[0]) await loadBlob(e.target.files[0]); });
  $("#dload").onclick = guard(async () => {
    const url = $("#durl").value.trim(); if (!url) return;
    const res = await fetch(`/api/v1/tools/fetch-image?url=${encodeURIComponent(url)}`, { credentials: "same-origin" });
    if (!res.ok) throw new Error((await res.json().catch(() => ({})))?.error?.message || `HTTP ${res.status}`);
    await loadBlob(await res.blob());
  });
  $("#dcurrent").onclick = guard(async () => {
    if (!a.icon) throw new Error("Die App hat noch kein Icon");
    const same = a.icon.startsWith(location.origin) || a.icon.startsWith("/");
    const res = await fetch(same ? a.icon : `/api/v1/tools/fetch-image?url=${encodeURIComponent(a.icon)}`, { credentials: "same-origin" });
    await loadBlob(await res.blob());
  });
  $("#dclear").onclick = () => { st.img = null; draw(); };
  const onPaste = guard(async (e) => {
    if (!location.hash.startsWith("#/icon/")) return document.removeEventListener("paste", onPaste);
    const item = [...(e.clipboardData?.items || [])].find((i) => i.type.startsWith("image/"));
    if (item) { e.preventDefault(); await loadBlob(item.getAsFile()); return; }
    const txt = e.clipboardData?.getData("text") || "";
    if (/^https?:\/\//.test(txt) && document.activeElement !== $("#durl")) { $("#durl").value = txt; $("#dload").click(); }
  });
  document.addEventListener("paste", onPaste);

  $("#zoom").oninput = (e) => { st.zoom = e.target.value / 100; draw(); };
  $("#dcenter").onclick = () => { st.x = 0; st.y = 0; draw(); };
  $("#dfit").onclick = () => { st.zoom = 1; st.x = 0; st.y = 0; $("#zoom").value = 100; draw(); };
  $("#dfill").onclick = () => {
    if (!st.img) return;
    st.zoom = Math.max(1024 / st.img.width, 1024 / st.img.height) / base(); st.x = 0; st.y = 0;
    $("#zoom").value = Math.min(400, st.zoom * 100); draw();
  };
  // drag + wheel
  let drag = null;
  cv.onpointerdown = (e) => { drag = { x: e.clientX, y: e.clientY, sx: st.x, sy: st.y }; cv.setPointerCapture(e.pointerId); };
  cv.onpointermove = (e) => {
    if (!drag) return;
    const k = 1024 / cv.getBoundingClientRect().width;
    st.x = drag.sx + (e.clientX - drag.x) * k; st.y = drag.sy + (e.clientY - drag.y) * k; draw();
  };
  cv.onpointerup = cv.onpointercancel = () => { drag = null; };
  cv.onwheel = (e) => {
    e.preventDefault();
    st.zoom = Math.min(4, Math.max(0.05, st.zoom * (e.deltaY < 0 ? 1.06 : 1 / 1.06)));
    $("#zoom").value = st.zoom * 100; draw();
  };
  // background + text
  const bind = (sel, key, conv = (v) => v, ev = "input") => { $(sel).addEventListener(ev, (e) => { st[key] = conv(e.target.type === "checkbox" ? e.target.checked : e.target.value); draw(); }); };
  bind("#bg1", "bg1"); bind("#bg2", "bg2"); bind("#grad", "grad", Boolean, "change"); bind("#angle", "angle", Number);
  bind("#dtext", "text"); bind("#tcolor", "tcolor"); bind("#tweight", "tweight", String, "change");
  bind("#tshadow", "shadow", Boolean, "change"); bind("#tsize", "tsize", Number); bind("#ty", "ty", Number);
  $$(".swatch").forEach((b) => b.onclick = () => { st.bg1 = b.dataset.c; $("#bg1").value = b.dataset.c; draw(); });

  const render = (px) => new Promise((ok) => {
    const c = document.createElement("canvas"); c.width = c.height = px;
    paint(c.getContext("2d"), px); c.toBlob(ok, "image/png");
  });
  $("#dpng").onclick = guard(async () => {
    const blob = await render(1024);
    const l = document.createElement("a"); l.href = URL.createObjectURL(blob); l.download = `${a.name}-icon.png`; l.click();
  });
  $("#dsave").onclick = guard(async () => {
    if (!st.img && !st.text && !st.grad && st.bg1.toLowerCase() === "#ffffff") throw new Error("Das Icon ist noch leer");
    $("#dsave").disabled = true;
    try {
      const fd = new FormData();
      for (const px of ICON_SIZES) fd.append(`icon_${px}`, await render(px), `${px}.png`);
      fd.append("inject", $("#sinject").checked ? "all" : "none");
      fd.append("auto_inject", $("#sauto").checked ? "1" : "0");
      await api("PUT", `/apps/${a.id}/custom-icon`, fd);
      toast($("#sinject").checked ? "Gespeichert – Icon wird in die Builds eingebaut" : "Gespeichert");
      location.hash = `#/app/${a.id}`;
    } finally { $("#dsave").disabled = false; }
  });
  draw();
}

// --------------------------------------------------------------- upload --
function renderUpload() {
  view.innerHTML = `<div class="head"><div><h1>Upload</h1><div class="muted">IPAs direkt hochladen – Bundle-ID, Version und Icon werden automatisch gelesen.</div></div></div>
    <div class="panel"><label class="drop" id="drop"><input type="file" accept=".ipa" multiple hidden id="files">
      <div style="font-size:1.1rem;font-weight:600">IPA hierher ziehen oder klicken</div><div class="muted small">mehrere Dateien möglich · große Dateien werden in Teilen übertragen und lassen sich nach Abbruch fortsetzen (Datei einfach erneut wählen)</div></label>
      <div style="margin-top:14px"><label>Release-Notizen (optional, für alle Dateien)</label><input id="notes"></div></div>
    <div id="results" class="stack"></div>`;
  const drop = $("#drop");
  const handle = (files) => [...files].forEach(uploadOne);
  $("#files").onchange = (e) => handle(e.target.files);
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); handle(e.dataTransfer.files); };
}

// Chunked, resumable upload: survives proxy limits, flaky Wi-Fi and page reloads
const sessKey = (f) => `ipas-upload:${f.name}:${f.size}:${f.lastModified}`;
const lsGet = (k) => { try { return localStorage.getItem(k); } catch { return null; } };
const lsSet = (k, v) => { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch { /* */ } };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function uploadOne(file) {
  const row = document.createElement("div");
  row.className = "panel";
  row.innerHTML = `<div class="row spread"><b>${esc(file.name)}</b><span class="muted small">${fmtSize(file.size)}</span></div>
    <progress max="100" value="0"></progress>
    <div class="row spread"><div class="small muted" data-s>startet …</div><div class="row">
      <button class="sm" data-resume hidden>Fortsetzen</button><button class="sm danger" data-cancel>Abbrechen</button></div></div>`;
  $("#results").prepend(row);
  const status = (h) => { $("[data-s]", row).innerHTML = h; };
  const bar = $("progress", row);
  let cancelled = false, sid = null;

  $("[data-cancel]", row).onclick = async () => {
    cancelled = true;
    if (sid) { await api("DELETE", `/uploads/sessions/${sid}`).catch(() => {}); lsSet(sessKey(file), null); }
    status("abgebrochen"); $("[data-cancel]", row).hidden = true; $("[data-resume]", row).hidden = true;
  };
  $("[data-resume]", row).onclick = () => { $("[data-resume]", row).hidden = true; run(); };

  async function run() {
    try {
      // 1) find or create the session
      let sess = null;
      sid = lsGet(sessKey(file));
      if (sid) sess = await api("GET", `/uploads/sessions/${sid}`).catch(() => null);
      if (!sess || sess.status === "error") {
        sess = await api("POST", "/uploads/sessions", { filename: file.name, size: file.size, notes: $("#notes").value });
        sid = sess.id; lsSet(sessKey(file), sid);
      } else if (sess.received) {
        status(`setze bei ${fmtSize(sess.received)} fort …`);
      }
      // 2) send chunks
      let offset = sess.received || 0;
      const chunk = sess.chunk_size || 32 * 1024 * 1024;
      const t0 = Date.now(), start = offset;
      while (offset < file.size && sess.status === "uploading") {
        if (cancelled) return;
        const blob = file.slice(offset, Math.min(offset + chunk, file.size));
        let tries = 0;
        for (;;) {
          const res = await fetch(`/api/v1/uploads/sessions/${sid}?offset=${offset}`, {
            method: "PUT", headers: { "X-Requested-With": "ipa-source" }, body: blob, credentials: "same-origin",
          }).catch(() => null);
          if (res && res.ok) { offset = (await res.json()).received; break; }
          if (res && res.status === 409) { offset = (await res.json()).received; break; }   // server knows better
          if (res && res.status < 500 && res.status !== 408 && res.status !== 429) {
            const d = await res.json().catch(() => ({}));
            throw new Error(d?.error?.message || `HTTP ${res.status}`);
          }
          if (++tries > 6) throw new Error("Verbindung mehrfach abgebrochen");
          status(`Verbindungsfehler – neuer Versuch ${tries}/6 …`);
          await sleep(Math.min(30000, 1000 * 2 ** tries));
        }
        bar.value = (offset / file.size) * 100;
        const rate = (offset - start) / Math.max(1, (Date.now() - t0) / 1000);
        const eta = rate > 0 ? Math.round((file.size - offset) / rate) : 0;
        status(`${fmtSize(offset)} von ${fmtSize(file.size)} · ${fmtSize(rate)}/s · noch ca. ${eta > 90 ? Math.round(eta / 60) + " min" : eta + " s"}`);
      }
      // 3) finish + wait for import
      status("wird verarbeitet (Prüfsumme, Metadaten) …");
      sess = await api("POST", `/uploads/sessions/${sid}/complete`);
      while (sess.status === "processing") { await sleep(2000); sess = await api("GET", `/uploads/sessions/${sid}`); }
      lsSet(sessKey(file), null);
      $("[data-cancel]", row).hidden = true;
      if (sess.status === "done") {
        bar.value = 100;
        status(`✓ <a href="#/app/${sess.app.id}">${esc(sess.app.name)}</a> ${esc(sess.build.version)} (${esc(sess.build.build_version)}) importiert`);
      } else {
        status(`<span class="badge err">Fehler</span> ${esc(sess.error)}`);
      }
    } catch (e) {
      if (cancelled) return;
      status(`<span class="badge err">Unterbrochen</span> ${esc(e.message)}`);
      $("[data-resume]", row).hidden = false;
    }
  }
  run();
}

// ----------------------------------------------------------------- feed --
async function renderFeed() {
  const [u, s, { tokens }] = await Promise.all([api("GET", "/feed"), api("GET", "/settings"), api("GET", "/tokens")]);
  view.innerHTML = `<div class="head"><div><h1>Feed &amp; Zugänge</h1></div></div>
    <div class="panel stack"><h2>Source-URLs</h2>
      <div><label>AltStore / SideStore / Feather</label>${copyBox(u.apps_json)}</div>
      <div><label>KravaSign / ESign (Legacy-Format)</label>${copyBox(u.legacy_json)}</div>
      <div class="row spread"><label class="check"><input type="checkbox" id="priv" ${u.private ? "checked" : ""}> Privater Feed (URL enthält geheimen Schlüssel)</label>
        <button class="sm" id="rotate" ${u.private ? "" : "disabled"}>Schlüssel neu erzeugen</button></div></div>

    <div class="panel stack"><h2>WebDAV</h2>
      <div class="muted small">IPAs in den Hauptordner legen → werden automatisch importiert. Jede App erscheint als Ordner mit ihren Builds. Anmeldung mit Benutzer + Passwort oder Benutzer + API-Token.</div>
      ${copyBox(u.webdav_url)}</div>

    <div class="panel stack"><h2>GitHub-Webhook (optional)</h2>
      <div class="muted small">Im App-Repo unter Settings → Webhooks eintragen (Content type <code>application/json</code>, Events: <i>Releases</i> und <i>Workflow runs</i>). Dann wird sofort nach einem Build synchronisiert – der Server muss dafür aus dem Internet erreichbar sein.</div>
      <div><label>Payload URL</label>${copyBox(u.webhook_url)}</div><div><label>Secret</label>${copyBox(s.webhook_secret)}</div></div>

    <div class="panel"><div class="row spread"><h2>API-Tokens</h2><button class="primary sm" id="newtok">Token erstellen</button></div>
      <div class="muted small" style="margin-bottom:10px">Für CI-Uploads (<code>upload</code>), Lesezugriff (<code>read</code>) oder die iOS-App (<code>full</code>).</div>
      ${tokens.length ? `<table><thead><tr><th>Name</th><th>Scope</th><th>Präfix</th><th>Zuletzt benutzt</th><th></th></tr></thead><tbody>${tokens.map((t) => `<tr>
        <td>${esc(t.name)}</td><td><span class="badge">${esc(t.scope)}</span></td><td class="mono small">${esc(t.prefix)}…</td>
        <td class="small">${fmtDate(t.last_used_at)}</td><td style="text-align:right"><button class="sm danger" data-del="${t.id}">Widerrufen</button></td></tr>`).join("")}</tbody></table>` : '<div class="muted">Keine Tokens.</div>'}</div>

    <div class="panel stack"><h2>Upload aus GitHub Actions</h2>
      <div class="muted small">Alternative zum Abholen: Der Workflow schiebt die IPA direkt hierher (Secrets <code>IPA_SOURCE_URL</code> und <code>IPA_SOURCE_TOKEN</code> mit Scope <i>upload</i>).</div>
      <pre class="mono small panel" style="overflow:auto;margin:0">- name: Upload to ipa-source
  run: |
    curl -fsS -X PUT -H "Authorization: Bearer \${{ secrets.IPA_SOURCE_TOKEN }}" \\
      --data-binary @build/MyApp.ipa \\
      "\${{ secrets.IPA_SOURCE_URL }}/api/v1/uploads?filename=MyApp.ipa&amp;notes=\${{ github.sha }}"</pre></div>`;

  $("#priv").onchange = guard(async (e) => { await api("PATCH", "/settings", { feed_private: e.target.checked }); renderFeed(); });
  $("#rotate").onclick = guard(async () => { if (!confirm("Alte Feed-URLs funktionieren danach nicht mehr. Fortfahren?")) return; await api("POST", "/feed/rotate-key"); renderFeed(); });
  $("#newtok").onclick = () => modal(`<h2>API-Token erstellen</h2><div class="form">
      <div><label>Name</label><input name="name" required placeholder="z. B. GitHub Actions"></div>
      <div><label>Scope</label><select name="scope"><option value="upload">upload – nur hochladen</option><option value="read">read – nur lesen</option><option value="full">full – alles</option></select></div></div>
      <div class="row" style="margin-top:16px"><button class="primary">Erstellen</button><button value="cancel">Abbrechen</button></div>`,
    async (f, form) => {
      const r = await api("POST", "/tokens", { name: f.get("name"), scope: f.get("scope") });
      form.innerHTML = `<h2>Token erstellt</h2><p class="muted">Wird nur jetzt angezeigt:</p>${copyBox(r.token)}<div class="row" style="margin-top:16px"><button value="cancel">Fertig</button></div>`;
      form.onsubmit = null; $("#modal").addEventListener("close", renderFeed, { once: true });
      return false;
    });
  $$("[data-del]").forEach((b) => b.onclick = guard(async () => { if (!confirm("Token widerrufen?")) return; await api("DELETE", `/tokens/${b.dataset.del}`); renderFeed(); }));
}

// ------------------------------------------------------------- settings --
async function renderSettings() {
  const [s, gh] = await Promise.all([api("GET", "/settings"), api("GET", "/github")]);
  const field = (k, label, extra = "") => `<div><label>${label}</label><input name="${k}" value="${esc(s[k])}" ${extra}></div>`;
  view.innerHTML = `<div class="head"><h1>Einstellungen</h1></div>
    <div class="panel stack"><h2>GitHub-Verbindung</h2>
      <div>${gh.connected ? `✓ Verbunden als <b>${esc(gh.user || "?")}</b>${gh.from_env ? ' <span class="badge">aus ENV</span>' : ""}` : gh.error ? `<span class="badge err">${esc(gh.error)}</span>` : "Nicht verbunden"}</div>
      <div class="form">
        <div class="full"><label>Personal Access Token (fine-grained: Contents + Actions read-only; classic: <code>repo</code>)</label>
          <div class="copy"><input id="pat" type="password" placeholder="github_pat_…"><button id="savepat">Speichern</button></div></div>
        <div class="full"><label>Oder per GitHub-Login (OAuth-App mit aktiviertem Device Flow – Client-ID eintragen)</label>
          <div class="copy"><input id="clientid" value="${esc(s.github_client_id)}" placeholder="Ov23li…"><button id="device">Mit GitHub anmelden</button></div></div>
      </div>
      ${gh.connected && !gh.from_env ? '<div><button class="danger sm" id="disconnect">Verbindung trennen</button></div>' : ""}</div>

    <form class="panel" id="feedform"><h2>Feed</h2><div class="form">
      ${field("feed_name", "Name")}${field("feed_identifier", "Identifier")}
      ${field("feed_subtitle", "Untertitel")}${field("developer", "Standard-Entwicklername")}
      ${field("feed_tint", "Farbe")}${field("feed_website", "Website")}
      ${field("feed_icon_url", "Icon-URL")}${field("feed_header_url", "Header-URL (3:2)")}
      <div class="full"><label>Beschreibung</label><textarea name="feed_description">${esc(s.feed_description)}</textarea></div>
    </div><h2 style="margin-top:20px">Server</h2><div class="form">
      ${field("base_url", "Öffentliche Basis-URL", `placeholder="${esc(s.base_url_effective)}"`)}
      ${field("sync_interval", "Sync-Intervall (Sekunden)", 'type="number" min="60"')}
      <div class="full"><button class="primary">Speichern</button></div></div></form>

    <form class="panel" id="pwform"><h2>Passwort ändern</h2><div class="form">
      <div><label>Aktuelles Passwort</label><input type="password" name="current" required autocomplete="current-password"></div>
      <div><label>Neues Passwort</label><input type="password" name="new" minlength="8" required autocomplete="new-password"></div>
      <div class="full"><button>Ändern</button></div></div></form>`;

  $("#savepat").onclick = guard(async () => {
    const r = await api("PUT", "/github/token", { token: $("#pat").value.trim() });
    toast(`Verbunden als ${r.user}`); renderSettings();
  });
  $("#disconnect")?.addEventListener("click", guard(async () => { await api("DELETE", "/github/token"); renderSettings(); }));
  $("#device").onclick = guard(async () => {
    await api("PATCH", "/settings", { github_client_id: $("#clientid").value.trim() });
    const d = await api("POST", "/github/device/start");
    let stop = false;
    modal(`<h2>Mit GitHub verbinden</h2><p>Öffne <a href="${esc(d.verification_uri)}" target="_blank" rel="noopener">${esc(d.verification_uri)}</a> und gib diesen Code ein:</p>
      <div class="code-big" data-copy="${esc(d.user_code)}">${esc(d.user_code)}</div><p class="muted small" id="devstate">Warte auf Bestätigung …</p>
      <div class="row"><button value="cancel">Abbrechen</button></div>`);
    $("#modal").addEventListener("close", () => { stop = true; }, { once: true });
    let interval = (d.interval || 5) * 1000;
    while (!stop) {
      await new Promise((r) => setTimeout(r, interval));
      if (stop) break;
      const p = await api("POST", "/github/device/poll", { handle: d.handle });
      if (p.status === "connected") { $("#modal").close(); toast(`Verbunden als ${p.user}`); renderSettings(); break; }
      if (p.status === "slow_down") interval += 5000;
      else if (p.status !== "authorization_pending") { $("#devstate").textContent = `Abgebrochen: ${p.status}`; break; }
    }
  });
  $("#feedform").onsubmit = guard(async (e) => {
    e.preventDefault();
    await api("PATCH", "/settings", Object.fromEntries(new FormData(e.target)));
    toast("Gespeichert");
  });
  $("#pwform").onsubmit = guard(async (e) => {
    e.preventDefault();
    await api("PUT", "/me/password", Object.fromEntries(new FormData(e.target)));
    e.target.reset(); toast("Passwort geändert");
  });
}

// ------------------------------------------------------------------ log --
async function renderLog() {
  const draw = async () => {
    const { events } = await api("GET", "/events?limit=200");
    view.innerHTML = `<div class="head"><h1>Protokoll</h1></div><div class="panel"><table><tbody>${events.map((e) => `<tr>
      <td class="small muted" style="white-space:nowrap">${fmtDate(e.ts)}</td>
      <td>${e.level === "error" ? '<span class="badge err">Fehler</span>' : '<span class="badge">Info</span>'}</td>
      <td class="small">${esc(e.message)}</td></tr>`).join("")}</tbody></table></div>`;
  };
  await draw();
  timer = setInterval(guard(draw), 5000);
}

boot();
