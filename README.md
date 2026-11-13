# ipa-source

Selbst gehostete App-Source für **Feather, KravaSign, ESign, AltStore und SideStore**:
unsignierte IPAs aus GitHub-Releases, Actions-Artefakten, Web-Upload oder WebDAV,
verwaltet über ein Web-Admin mit REST-API.

- **GitHub-Quellen:** Releases und/oder Actions-Artefakte. Über einen Token oder GitHub-Login (Device Flow) verbunden,
  Repos werden aufgelistet und auf IPAs gescannt.
- **Upload** per Drag & Drop im Browser oder per `curl` aus CI
- **WebDAV** unter `/dav/`: IPA hineinziehen → Import; jede App ist ein Ordner mit ihren Builds
- **Feeds**: `apps.json` (AltStore-Spezifikation 2.0) und `apps-legacy.json` (strenge Parser wie
  KravaSign/ESign). Optional privat über einen geheimen Schlüssel in der URL.
- **Automatik**: Bundle-ID, Version, Build, Mindest-iOS, Icon (inkl. Apple-CgBI-PNG-Fix),
  Privacy-Texte und Entitlements werden aus der IPA gelesen.
- **Aufbewahrung** pro Quelle (N Releases / M Artefakte), einzelne Builds fixierbar.
  Abgelaufene Artefakte bleiben lokal erhalten.
- **Webhook** für sofortigen Sync nach Release/Workflow-Run
- **API-first** (`/api/v1`, Bearer-Tokens mit Scopes), damit eine iOS-App später dieselbe API nutzen kann.
  Spezifikation: [`docs/openapi.yaml`](docs/openapi.yaml)

Nur Python-Standardbibliothek plus Starlette/Uvicorn, SQLite, kein Node-Build.

## Start

```bash
git clone https://github.com/kernelguru/ipa-source && cd ipa-source
cp .env.example .env        # BASE_URL anpassen
docker compose up -d --build
```

Dann `http://<host>:8080` öffnen und den Admin anlegen. Alternativ setzt du
`ADMIN_USER`/`ADMIN_PASSWORD` in der `.env`.

Hinter einem Reverse-Proxy mit HTTPS `COOKIE_SECURE=1` setzen. `BASE_URL` muss die Adresse
sein, unter der die iPhones den Server erreichen, denn alle Download-Links werden daraus gebaut.
`BASE_URL` lässt sich auch in den Einstellungen überschreiben.

### Fertiges Image

Der Workflow `.github/workflows/docker.yml` baut bei jedem Push nach `main`
`ghcr.io/kernelguru/ipa-source:latest` (amd64 + arm64). Weil das Repo privat ist, ist auch
das Image privat. Auf dem Host einmalig mit `docker login ghcr.io` anmelden (PAT mit
`read:packages`) und in der `docker-compose.yml` `image:` statt `build:` verwenden.

## GitHub verbinden

**Variante A – Token:** Unter *Einstellungen* einen Personal Access Token eintragen.
- fine-grained: Repository access auf die App-Repos, Permissions **Contents: Read-only** und **Actions: Read-only**
- classic: Scope `repo`

**Variante B – „Mit GitHub anmelden“ (Device Flow):** Einmalig unter
*GitHub → Settings → Developer settings → OAuth Apps* eine App anlegen (Homepage/Callback beliebig,
z. B. die BASE_URL) und **Enable Device Flow** anhaken. Die Client-ID in den Einstellungen eintragen,
auf „Mit GitHub anmelden“ klicken und den angezeigten Code auf github.com bestätigen.
Ein Callback auf den Server ist nicht nötig, das funktioniert also auch rein im LAN.

Danach unter *GitHub-Quellen → Aus meinen Repos wählen* die Repos scannen und hinzufügen.

## Versionierung

Version und Build kommen immer aus der `Info.plist` der IPA (`CFBundleShortVersionString`,
`CFBundleVersion`). Gibt es gleiche Version + Build mehrfach, gilt die Priorität Release > Upload/WebDAV > Artefakt.
Für CI-Builds die Build-Nummer hochzählen, z. B.
`xcodebuild … CURRENT_PROJECT_VERSION=${{ github.run_number }}`.

## Upload aus CI

Token mit Scope `upload` erstellen (*Feed & Zugänge*), dann:

```bash
curl -fsS -X PUT -H "Authorization: Bearer $IPA_SOURCE_TOKEN" \
  --data-binary @MyApp.ipa \
  "$IPA_SOURCE_URL/api/v1/uploads?filename=MyApp.ipa&notes=Build%20123"
```

## WebDAV

`https://<host>/dav/`. Anmeldung mit Benutzer + Passwort oder Benutzer + API-Token
(Scope `full` oder `upload`). IPAs in den **Hauptordner** legen. Sie werden innerhalb weniger Sekunden
importiert und erscheinen dann im Ordner der App. Builds lassen sich dort auch löschen.
Getestet mit curl; Finder, Windows-Explorer, Cyberduck und rclone nutzen dieselben Methoden.

## Daten

Alles liegt im Volume `/data`: `ipa-source.db` (SQLite), `storage/` (IPAs + Icons),
`inbox/` (WebDAV-Eingang), `secret.key` (Cookie-Signatur).
Backup = Ordner kopieren.

## Entwicklung

```bash
pip install -r requirements.txt
DATA_DIR=./data BASE_URL=http://127.0.0.1:8080 uvicorn app.main:app --reload --port 8080
python tests/test_api.py      # End-to-End-Test gegen einen frischen Server
```
