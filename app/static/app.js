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
const originLabel = { release: "Release", artifact: "Artefakt", upload: "Upload", webdav: "WebDAV" };

function modal(html, onSubmit) {
  const dlg = $("#modal"), form = $("#modal-body");
  form.innerHTML = html;
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
  upload: renderUpload, feed: renderFeed, settings: renderSettings, log: renderLog,
};
let timer = null;
async function route() {
  clearInterval(timer);
  const [, name = "apps", arg] = location.hash.split("/");
  $$("nav a").forEach((a) => a.classList.toggle("active", a.dataset.nav === (name === "app" ? "apps" : name === "github" ? "sources" : name)));
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
          <div class="title">${esc(a.name)} ${a.visible ? "" : '<span class="badge">versteckt</span>'}</div>
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
      <div><h1>${esc(a.name)}</h1><div class="muted mono small">${esc(a.bundle_id)}</div></div></div>
      <div class="row"><label class="btn">Icon hochladen<input type="file" accept="image/png" id="iconfile" hidden></label>
      <button class="danger" id="delapp">App löschen</button></div></div>

    <div class="panel"><h2>Builds</h2>
      <table><thead><tr><th>Version</th><th>Herkunft</th><th>Datum</th><th>Größe</th><th>Im Feed</th><th>Fixiert</th><th></th></tr></thead><tbody>
      ${a.builds.map((b) => `<tr data-id="${b.id}">
        <td><b>${esc(b.version)}</b> <span class="muted">(${esc(b.build_version)})</span>${b.min_os ? `<div class="muted small">ab iOS ${esc(b.min_os)}</div>` : ""}</td>
        <td><span class="badge ${b.origin}">${originLabel[b.origin]}</span><div class="muted small">${esc(b.repo || "")} ${b.origin === "release" ? esc(b.origin_ref) : ""}</div></td>
        <td class="small">${fmtDate(b.date)}</td><td class="small">${fmtSize(b.size)}</td>
        <td><input type="checkbox" data-f="visible" ${b.visible ? "checked" : ""}></td>
        <td><input type="checkbox" data-f="pinned" ${b.pinned ? "checked" : ""} title="Wird von der Aufbewahrung nicht gelöscht"></td>
        <td class="row" style="justify-content:flex-end"><a class="btn sm" href="${esc(b.download_url)}">Laden</a>
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
    $("[data-notes]", tr).onclick = () => modal(`<h2>Release-Notizen</h2><textarea name="notes" style="min-height:180px">${esc(b.notes)}</textarea>
      <div class="row" style="margin-top:12px"><button class="primary">Speichern</button><button value="cancel">Abbrechen</button></div>`,
      async (f) => { await api("PATCH", `/builds/${bid}`, { notes: f.get("notes") }); renderApp(id); });
  });
}

// -------------------------------------------------------------- sources --
function sourceForm(s = {}) {
  const v = (k, d) => esc(s[k] ?? d);
  const c = (k, d) => (s[k] ?? d) ? "checked" : "";
  return `<h2>${s.id ? "Quelle bearbeiten" : "GitHub-Quelle hinzufügen"}</h2><div class="form">
    <div class="full"><label>Repository (owner/name)</label><input name="repo" value="${v("repo", "")}" required ${s.id ? "readonly" : ""}></div>
    <label class="check"><input type="checkbox" name="use_releases" ${c("use_releases", true)}> Releases</label>
    <label class="check"><input type="checkbox" name="use_artifacts" ${c("use_artifacts", false)}> Actions-Artefakte</label>
    <label class="check"><input type="checkbox" name="include_prereleases" ${c("include_prereleases", false)}> Pre-Releases</label>
    <label class="check"><input type="checkbox" name="enabled" ${c("enabled", true)}> Aktiv</label>
    <div><label>Dateimuster (Release / im Artefakt)</label><input name="asset_pattern" value="${v("asset_pattern", "*.ipa")}"></div>
    <div><label>Artefakt-Name (Glob)</label><input name="artifact_name" value="${v("artifact_name", "*")}"></div>
    <div><label>Nur Branch (leer = alle)</label><input name="artifact_branch" value="${v("artifact_branch", "")}"></div>
    <div><label>Releases behalten</label><input name="keep_releases" type="number" min="0" value="${v("keep_releases", 5)}"></div>
    <div><label>Artefakte behalten</label><input name="keep_artifacts" type="number" min="0" value="${v("keep_artifacts", 3)}"></div>
  </div><div class="row" style="margin-top:16px"><button class="primary">Speichern</button><button value="cancel">Abbrechen</button></div>`;
}
const sourcePayload = (f) => ({
  repo: f.get("repo"), use_releases: f.get("use_releases") === "on", use_artifacts: f.get("use_artifacts") === "on",
  include_prereleases: f.get("include_prereleases") === "on", enabled: f.get("enabled") === "on",
  asset_pattern: f.get("asset_pattern"), artifact_name: f.get("artifact_name"), artifact_branch: f.get("artifact_branch"),
  keep_releases: +f.get("keep_releases"), keep_artifacts: +f.get("keep_artifacts"),
});

async function renderSources() {
  const [{ sources }, gh] = await Promise.all([api("GET", "/sources"), api("GET", "/github")]);
  view.innerHTML = `<div class="head"><div><h1>GitHub-Quellen</h1>
      <div class="muted">${gh.connected ? `Verbunden als <b>${esc(gh.user || "?")}</b>` : `Nicht mit GitHub verbunden – <a href="#/settings">verbinden</a>`}</div></div>
    <div class="row"><a class="btn" href="#/github">Aus meinen Repos wählen</a><button class="primary" id="add">Manuell hinzufügen</button></div></div>
    <div class="panel">${sources.length ? `<table><thead><tr><th>Repository</th><th>Holt</th><th>Builds</th><th>Letzter Sync</th><th></th></tr></thead><tbody>
      ${sources.map((s) => `<tr data-id="${s.id}">
        <td><b>${esc(s.repo)}</b> ${s.enabled ? "" : '<span class="badge">inaktiv</span>'}${s.syncing ? ' <span class="badge">läuft …</span>' : ""}</td>
        <td class="small">${[s.use_releases && "Releases", s.use_artifacts && `Artefakte${s.artifact_branch ? ` (${esc(s.artifact_branch)})` : ""}`].filter(Boolean).join(", ")}</td>
        <td>${s.build_count}</td>
        <td class="small">${fmtDate(s.last_sync_at)}<div class="muted">${esc(s.last_status)}</div>${s.last_error ? `<div class="badge err" title="${esc(s.last_error)}">Fehler</div>` : ""}</td>
        <td class="row" style="justify-content:flex-end"><button class="sm" data-sync>Sync</button><button class="sm" data-edit>Bearbeiten</button><button class="sm danger" data-del>Entfernen</button></td></tr>`).join("")}
      </tbody></table>` : `<div class="muted">Noch keine Quellen.</div>`}</div>`;
  $("#add").onclick = () => modal(sourceForm(), async (f) => { await api("POST", "/sources", sourcePayload(f)); toast("Quelle angelegt, Sync läuft"); renderSources(); });
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

// --------------------------------------------------------------- upload --
function renderUpload() {
  view.innerHTML = `<div class="head"><div><h1>Upload</h1><div class="muted">IPAs direkt hochladen – Bundle-ID, Version und Icon werden automatisch gelesen.</div></div></div>
    <div class="panel"><label class="drop" id="drop"><input type="file" accept=".ipa" multiple hidden id="files">
      <div style="font-size:1.1rem;font-weight:600">IPA hierher ziehen oder klicken</div><div class="muted small">mehrere Dateien möglich</div></label>
      <div style="margin-top:14px"><label>Release-Notizen (optional, für alle Dateien)</label><input id="notes"></div></div>
    <div id="results" class="stack"></div>`;
  const drop = $("#drop");
  const handle = (files) => [...files].forEach(uploadOne);
  $("#files").onchange = (e) => handle(e.target.files);
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); handle(e.dataTransfer.files); };
}

function uploadOne(file) {
  const row = document.createElement("div");
  row.className = "panel";
  row.innerHTML = `<div class="row spread"><b>${esc(file.name)}</b><span class="muted small">${fmtSize(file.size)}</span></div><progress max="100" value="0"></progress><div class="small muted" data-s>lädt …</div>`;
  $("#results").prepend(row);
  const fd = new FormData();
  fd.append("file", file); fd.append("notes", $("#notes").value);
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/v1/uploads");
  xhr.setRequestHeader("X-Requested-With", "ipa-source");
  xhr.upload.onprogress = (e) => { if (e.lengthComputable) $("progress", row).value = (e.loaded / e.total) * 100; };
  xhr.onload = () => {
    let d = {}; try { d = JSON.parse(xhr.responseText); } catch { /* */ }
    if (xhr.status === 201) {
      $("[data-s]", row).innerHTML = `✓ <a href="#/app/${d.app.id}">${esc(d.app.name)}</a> ${esc(d.build.version)} (${esc(d.build.build_version)}) importiert`;
    } else {
      $("[data-s]", row).innerHTML = `<span class="badge err">Fehler</span> ${esc(d?.error?.message || xhr.statusText)}`;
    }
  };
  xhr.onerror = () => { $("[data-s]", row).textContent = "Netzwerkfehler"; };
  xhr.send(fd);
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
