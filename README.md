# ipa-source

Selbst gehostete App-Source für **Feather, KravaSign, ESign, AltStore und SideStore**:
unsignierte IPAs aus GitHub-Releases, Actions-Artefakten, Web-Upload oder WebDAV,
verwaltet über ein Web-Admin mit REST-API.

- **Quellen überwachen:** eigene und fremde öffentliche Repos auf **GitHub, Codeberg, Gitea und Forgejo**.
  Neue Releases werden automatisch geladen. Die URL einfügen genügt, der Anbieter wird erkannt.
- **Link-Downloader** inkl. **iOS-Kurzbefehl**: Links aus Safari teilen, und der Server lädt die IPA.
- **GitHub-Quellen:** Releases und/oder Actions-Artefakte. Über einen Token oder GitHub-Login (Device Flow) verbunden,
  Repos werden aufgelistet und auf IPAs gescannt.
- **Upload** per Drag & Drop im Browser (in Teilen, fortsetzbar, auch für IPAs mit mehreren GB) oder per `curl` aus CI
- **WebDAV** unter `/dav/`: IPA hineinziehen → Import; jede App ist ein Ordner mit ihren Builds
- **Feeds**: `apps.json` (AltStore-Spezifikation 2.0) und `apps-legacy.json` (strenge Parser wie
  KravaSign/ESign). Optional privat über einen geheimen Schlüssel in der URL.
- **Automatik**: Bundle-ID, Version, Build, Mindest-iOS, Icon (inkl. Apple-CgBI-PNG-Fix),
  Privacy-Texte und Entitlements werden aus der IPA gelesen.
- **Aufbewahrung** pro Quelle (N Releases / M Artefakte), einzelne Builds fixierbar.
  Abgelaufene Artefakte bleiben lokal erhalten.
- **Icon-Designer**: Logo aus Datei, Zwischenablage oder Internet-URL, mit Hintergrund/Verlauf und Text/Emoji.
  Das Icon wird optional **direkt in die IPA eingebaut** (auch bei großen IPAs in Sekunden), automatisch auch in neue Builds.
  Leere oder fehlende Logos werden erkannt.
- **Signieren & Installieren auf dem Server**: zsign signiert IPAs mit deinem Zertifikat (.p12 + Profil). Installation
  direkt aus Safari per Link (OTA, `itms-services`) oder per USB an einem iPhone am Server (libimobiledevice). Damit
  fallen Signier-Apps auf dem iPhone weg, auch bei IPAs mit mehreren GB.
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

## Fremde Quellen (GitHub, Codeberg, Gitea, Forgejo)

Unter **Quellen → Quelle hinzufügen** die Repo-URL einfügen, z. B. `https://github.com/claration/Feather` oder
`https://codeberg.org/owner/app`. GitHub oder Gitea/Forgejo wird automatisch erkannt. Für selbst gehostete
Gitea/Forgejo-Server zählt die Adresse aus der URL. Für private Gitea-Repos kann pro Quelle ein Token hinterlegt
werden (verschlüsselt gespeichert). Actions-Artefakte gibt es nur bei GitHub.

Abfragen an GitHub nutzen ETags. Unveränderte Antworten zählen dadurch nicht gegen das Limit. Ohne GitHub-Token
erlaubt GitHub trotzdem nur 60 Abfragen pro Stunde, bei vielen Quellen also einen Token hinterlegen (auch für fremde
öffentliche Repos genügt einer ohne besondere Rechte).

## Link-Downloader und iOS-Kurzbefehl

Unter **Downloads** eine URL eingeben, oder per API bzw. Kurzbefehl `POST /api/v1/downloads` mit `{"url": "…"}`. Erkannt werden:

- direkte `.ipa`-Links. Bricht die Verbindung ab, wird per HTTP-Range fortgesetzt.
- `itms-services://`-Installationslinks (die IPA wird aus dem Manifest geholt)
- Repo-, Release- und Tag-Seiten von GitHub, Codeberg, Gitea und Forgejo (neueste IPA, danach „Repo überwachen“ möglich)
- Dropbox-Freigabelinks
- beliebige Webseiten, die auf IPAs verlinken: Ein Link wird direkt geladen, bei mehreren fragt die Oberfläche nach.

**Kurzbefehl:** Die Anleitung steht in der Oberfläche unter *Downloads* (mit deiner Adresse und einem Button für ein
Token mit Scope `upload`). Apple lässt nur signierte Kurzbefehl-Dateien zu, deshalb baust du ihn einmal in der
Kurzbefehle-App (3 Aktionen) und kannst ihn danach per iCloud-Link teilen.
Hinter Pangolin den Pfad `/api/v1/downloads` freigeben, er ist durch das Token geschützt.

## Versionierung

Version und Build kommen immer aus der `Info.plist` der IPA (`CFBundleShortVersionString`,
`CFBundleVersion`). Gibt es gleiche Version + Build mehrfach, gilt die Priorität Release > Upload/WebDAV > Artefakt.
Für CI-Builds die Build-Nummer hochzählen, z. B.
`xcodebuild … CURRENT_PROJECT_VERSION=${{ github.run_number }}`.

## Große Dateien

Uploads über die Weboberfläche laufen in Teilen (Standard 32 MB, `UPLOAD_CHUNK_MB`). Dadurch
stören Body-Limits und Timeouts des Reverse-Proxys nicht, und ein abgebrochener Upload
lässt sich fortsetzen. Dafür einfach dieselbe Datei erneut wählen. Ein Größenlimit gibt es standardmäßig nicht
(`MAX_UPLOAD_MB=0`), geprüft wird nur der freie Speicherplatz.

Native Clients nutzen dieselbe API: `POST /api/v1/uploads/sessions` → `PUT …/{id}?offset=N` pro Teil
→ `POST …/{id}/complete` → `GET …/{id}` bis `status` `done` ist (siehe `docs/openapi.yaml`).

## Icons

Apps ohne Logo oder mit leerem Logo (einfarbig/transparent) sind in der App-Liste markiert.
Unter **Icon gestalten** baust du ein Icon und speicherst es:

- **nur Feed**: Das Icon erscheint in Feather/KravaSign/AltStore.
- **in Builds einbauen**: Die IPA bekommt `AppIcon60x60@2x/@3x.png` und die iPad-Größen, und in der `Info.plist`
  wird `CFBundleIconName` entfernt. Damit nutzt iOS die neuen Dateien statt des Icons aus der `Assets.car`,
  die selbst unverändert bleibt. Es werden nur die neuen Dateien plus das ZIP-Inhaltsverzeichnis
  angehängt. Das dauert unabhängig von der IPA-Größe Sekunden, danach wird die Prüfsumme neu berechnet.
- **künftige Builds automatisch**: Neue Releases, Artefakte und Uploads bekommen das Icon beim Import.

Hinweise: Alternative Icons der App bleiben unverändert. Bei einer bereits installierten App zeigt iOS das neue
Icon manchmal erst nach einer Neuinstallation. Während das Icon eingebaut wird, sollte der Build nicht gerade
heruntergeladen werden.

## Signieren & Installieren

Unter **Signieren & Installieren** ein Zertifikat hinterlegen: `.p12`, ihr Passwort und das passende
`.mobileprovision`. Der Server prüft das Passwort und ob Zertifikat und Profil zusammenpassen.
Das Passwort wird verschlüsselt in der Datenbank gespeichert, die Dateien liegen mit Rechten 600 unter
`/data/certs`. Wer Zugriff auf das komplette Volume hat (inkl. `secret.key`), kann es entschlüsseln.

Auf der App-Seite bei einem Build auf **Installieren** tippen. Der Server signiert mit zsign (1,5 GB in etwa 10 s,
Stufe `SIGN_ZIP_LEVEL=0`). Passt die Bundle-ID nicht zur App-ID des Profils, wird sie automatisch angepasst.
Danach gibt es zwei Wege:

**1. Per Link (OTA):** Auf dem iPhone in Safari „Auf diesem Gerät installieren“ antippen, oder vom Rechner den Link
`https://<host>/install/<token>` aufs iPhone schicken. iOS lädt die App selbst vom Server, ganz ohne Signier-App.
Voraussetzungen:
- `BASE_URL` mit `https://` und gültigem Zertifikat (Let's Encrypt reicht)
- das iPhone muss im Provisioning-Profil stehen (Development/Ad Hoc), bei Enterprise-Profilen jedes Gerät
- hinter Pangolin/Authelia o. ä. den Pfad **`/install/*`** ohne Login freigeben (Bypass-Regel). Die Links enthalten
  einen geheimen Token.

**2. Per USB:** iPhone an den Docker-Host anschließen.
```bash
apt install usbmuxd          # auf dem Host
```
In der `docker-compose.yml` `- /var/run/usbmuxd:/var/run/usbmuxd` aktivieren, Container neu starten.
Unter *Signieren & Installieren → Geräte* erscheint das iPhone; einmal **Koppeln** und auf dem iPhone „Vertrauen“
bestätigen. Danach bei einer signierten IPA „Per USB installieren“ wählen. Läuft Docker in einer VM oder einem LXC,
muss das USB-Gerät dorthin durchgereicht werden. Alternativ `USBMUXD_EMBEDDED=1` mit
`devices: ["/dev/bus/usb:/dev/bus/usb"]` und `privileged: true`. Dann läuft usbmuxd im Container, und die Kopplung
liegt in `/data/lockdown`.

**Docker in einer Hyper-V-VM (Windows-Host):** Hyper-V reicht USB nicht durch. Zwei Wege:
- *usbipd-win*: Auf Windows `usbipd bind --busid <ID>`, in der VM `usbip attach -r <Windows-IP> -b <ID>`
  (Paket `usbip`/`linux-tools`, Modul `vhci-hcd`). Danach usbmuxd in der VM wie oben.
- *Apple Mobile Device Service* (kommt mit iTunes oder der App „Apple Geräte“): Er lauscht auf `127.0.0.1:27015`.
  Auf Windows nur für die VM weiterleiten, z. B.
  `netsh interface portproxy add v4tov4 listenaddress=<Windows-IP Richtung VM> listenport=27015 connectaddress=127.0.0.1 connectport=27015`,
  dazu eine Firewall-Regel nur für die VM-IP. Im Container dann `USBMUXD_SOCKET_ADDRESS=<Windows-IP>:27015` setzen.
  **Den Port nie im ganzen LAN öffnen**, sonst kann jeder im Netz auf das angeschlossene iPhone zugreifen.

Ohne USB funktioniert immer die Installation per Link (OTA).

Pro App und Zertifikat wird nur die neueste signierte Kopie aufbewahrt (`/data/signed`).

**Speicherbedarf beim Signieren:** Der Server entpackt die IPA selbst, zsign signiert den Ordner, danach wird
neu gepackt. Jede Datei wird gelöscht, sobald sie in der neuen IPA steckt. Nötig ist etwa *entpackte Größe +
größte Einzeldatei* an freiem Platz in `/data`. Die entpackte Größe kann deutlich über der IPA-Größe liegen.
Vor dem Start wird das geprüft und mit konkreten Zahlen gemeldet. ZIP64 (Dateien und IPAs über 4 GB) wird
unterstützt, Programme und Frameworks behalten bzw. bekommen das Ausführrecht.

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
