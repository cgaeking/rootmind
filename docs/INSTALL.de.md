# Installation im Detail

[English](INSTALL.md) | **Deutsch**

Die [README](../README.de.md#installation) hat die Kurzfassung. Hier steht alles Weitere: die Downloads, stille und verwaltete Installationen, Linux und WSL, der Umstieg von 1.x und der RootMind-Dienst.

## Installation

Jedes [Release](https://github.com/cgaeking/rootmind/releases/latest) enthält
diese Downloads für Windows (x64); die Linux-Pakete beschreibt
[Installation unter Linux](#installation-unter-linux):

- **[RootMind-Setup-x64.exe](https://github.com/cgaeking/rootmind/releases/latest/download/RootMind-Setup-x64.exe)**,
  für die meisten, unter Windows 10 oder 11: die Desktop-App und ihr
  Windows-Dienst. Starte die Datei und bestätige die Administrator-Abfrage
  einmal; Python ist nicht nötig. Sie installiert die App nach
  `%ProgramFiles%\RootMind` und richtet den
  [RootMind-Dienst](#der-rootmind-dienst) für das Konto ein, das bei Windows
  angemeldet ist.
- **[rootmind-backend-windows-x64.zip](https://github.com/cgaeking/rootmind/releases/latest/download/rootmind-backend-windows-x64.zip)**,
  für die Kommandozeile und Server: das Backend allein, ohne die App (siehe
  [Installation für Entwicklung und Server](DEVELOPMENT.de.md#installation-für-entwicklung-und-server)).
- **[rootmind.mcpb](https://github.com/cgaeking/rootmind/releases/latest/download/rootmind.mcpb)**,
  RootMind für Claude Desktop, nach der App zu installieren (siehe
  [RootMind in Claude Desktop nutzen](../README.de.md#rootmind-in-claude-desktop-nutzen)).

Zur Installation brauchst du ein Administratorkonto. Der Dienst gehört dem
Konto, das RootMind nutzt, deshalb richtet der Installer ihn nur ein, wenn er
als das bei Windows angemeldete Konto läuft. Der Installationsordner ist immer
`%ProgramFiles%\RootMind`, ein Ordner, den nur Administratoren ändern können;
einen anderen (etwa mit `/D=`) lehnt der Installer mit Exit-Code 10 ab.

**Stille und verwaltete Installationen.** winget, Intune, Configuration
Manager und ähnliche Werkzeuge führen den Installer oft still (`/S`) als
SYSTEM aus. Dann, und wenn ein Administrator die Abfrage bestätigt, während
ein anderes Konto angemeldet ist, installiert der Installer die App, aber
nicht den Dienst, und endet mit 0. Der Dienst wird eingerichtet, wenn das
Konto, das RootMind nutzt, die App zum ersten Mal öffnet: RootMind meldet „Der
RootMind-Dienst ist noch nicht eingerichtet“, und **Einrichten** richtet ihn
mit einer Bestätigung als Administrator für dieses Konto ein. Ein
Standardkonto kann ihn nicht einrichten; dafür braucht es ein
Administratorkonto, das RootMind selbst nutzt. Konnte der Installer den
Dienst aus einem anderen Grund nicht einrichten, endet er ebenfalls mit 0, und
RootMind zeigt das Problem und **Reparieren**. Das Ergebnis steht im
Deinstallationseintrag `HKLM\Software\Microsoft\Windows\CurrentVersion\Uninstall\RootMind`
(`RootMindServiceSetup`: `installed`, `skipped` oder `failed`, dazu
`RootMindServiceSetupReason` und bei einem Fehler `RootMindServiceSetupCode`).
Updates, die als SYSTEM laufen, aktualisieren einen vorhandenen Dienst an Ort
und Stelle.

Der Installer ist noch nicht codesigniert. Windows SmartScreen zeigt eventuell
„Der Computer wurde durch Windows geschützt“: Wähle **Weitere Informationen**
und dann **Trotzdem ausführen**. Ist Smart App Control aktiv, blockiert es den
Installer oder das Programm des RootMind-Dienstes eventuell ohne diese Wahl.
Smart App Control kennt keine Ausnahme für eine einzelne App; signierte
Releases, die geplant sind, vermeiden das (siehe die
[Code-Signing-Richtlinie](../CODE_SIGNING.md), englisch).

Starte danach RootMind und füge unter **Ordner** einen Ordner hinzu. Die
Indizierung beginnt innerhalb etwa einer Minute, und die Suche funktioniert
ohne API-Key, sobald der Ordner indiziert ist. Um Fragen zu stellen, trage
unter **Einstellungen** einen API-Key für Anthropic, OpenAI oder xAI ein oder wähle **Ollama** für ein lokales Modell, und
speichere ihn. Der Dienst hält den Index aktuell, auch während die App
geschlossen ist.

Die App folgt der Anzeigesprache von Windows (Deutsch oder Englisch);
Einstellungen → **Darstellung** → **Sprache** stellt sie um. Die Namen aus der
Oberfläche stehen in dieser README so, wie die deutsche Oberfläche sie zeigt.
**Über RootMind** im Menü zeigt die Versionen von App und Server und
verlinkt Releases und Issues.

### Installation unter Linux

Jedes Release enthält auch die App für Linux auf x86-64 mit glibc 2.35 oder
neuer (Ubuntu 22.04, Debian 12, Fedora 38, openSUSE Tumbleweed und neuer).
Wähle das Paket für deine Distribution:

| Distribution | Download | Installation |
| --- | --- | --- |
| Ubuntu, Debian und Ableger | [RootMind-amd64.deb](https://github.com/cgaeking/rootmind/releases/latest/download/RootMind-amd64.deb) | `sudo apt install ./RootMind-amd64.deb` |
| Fedora, openSUSE Tumbleweed | [RootMind-x86_64.rpm](https://github.com/cgaeking/rootmind/releases/latest/download/RootMind-x86_64.rpm) | `sudo dnf install ./RootMind-x86_64.rpm` (openSUSE: `sudo zypper install ./RootMind-x86_64.rpm`) |
| Jede andere Distribution | [RootMind-x86_64.AppImage](https://github.com/cgaeking/rootmind/releases/latest/download/RootMind-x86_64.AppImage) | `chmod +x RootMind-x86_64.AppImage`, dann starten |

Die Pakete installieren die App und ihr Backend nach `/usr/lib/RootMind` und
ziehen WebKitGTK 4.1 nach; nur dieser Schritt braucht root. Das AppImage
braucht keine Installation; es braucht FUSE 2 (das Paket `libfuse2`, unter
Ubuntu 24.04 `libfuse2t64`). Die Texterkennung (OCR, pro Ordner) nutzt das
Tesseract der Distribution: Die Pakete empfehlen es, `apt` installiert es
also mit; beim AppImage installiere `tesseract-ocr` und `tesseract-ocr-deu`
(Fedora: `tesseract`, `tesseract-langpack-deu`) selbst. Der Windows-Installer
bringt es mit. Prüfe den Download mit
`sha256sum --check --ignore-missing SHA256SUMS.txt` (siehe
[Downloads prüfen](#downloads-prüfen)).

**Starten** kannst du RootMind über das App-Menü oder im Terminal mit:

```sh
rootmind-desktop
```

Das .deb und das .rpm zeigen diesen Befehl bei der Installation an. Das
AppImage startet, wenn du die Datei ausführst.

**Windows-Subsystem für Linux.** Unter Windows 11 läuft RootMind auch in WSL,
das Fenster erscheint auf dem Windows-Desktop (WSLg). Der Dienst braucht in
WSL systemd: Trage Folgendes in `/etc/wsl.conf` ein, führe dann in Windows
`wsl --shutdown` aus und öffne die Distribution neu. Ohne systemd meldet
RootMind, dass es seinen Dienst nicht einrichten kann.

```ini
[boot]
systemd=true
```

**Was beim ersten Start passiert.** RootMind richtet seinen Hintergrunddienst
für dein Konto ein, ohne Rückfrage und ohne root: zwei
systemd-Benutzerdienste, `rootmind-backend.service` (REST-API und Indizierung)
und `rootmind-mcp-http.service` (MCP über HTTP), in
`~/.config/systemd/user/`. Sie starten eine Kopie des Backends in
`~/.local/share/rootmind/runtime/`, damit der Dienst weiterläuft, wenn das
AppImage geschlossen oder verschoben wird, und halten ihre Daten in
`~/.local/share/rootmind`. Startet eine neuere Version der App, bringt sie den
Dienst auf ihre Version. Scheitert die Einrichtung, sagt RootMind warum, und
**Einrichten** (oder **Reparieren**) versucht es erneut; Einstellungen →
**RootMind-Dienst** → **Details** zeigt den Zustand des Dienstes und das Ende
seiner `logs/install.log`. Danach fügst du wie unter Windows unter **Ordner**
einen Ordner hinzu.

Die Dienste starten, wenn du dich anmeldest, und enden mit deiner letzten
Sitzung. Damit sie auch laufen, wenn niemand angemeldet ist, zum Beispiel für
n8n auf demselben Computer, schalte einmal Lingering für dein Konto ein:

```sh
loginctl enable-linger "$USER"
```

**Aktualisieren:** Installiere das Paket der neuen Version (wie oben, oder
`sudo dnf upgrade ./RootMind-x86_64.rpm`) oder ersetze das AppImage; RootMind
aktualisiert seinen Dienst beim Start.

**Deinstallieren:** zuerst den Dienst, dann die App:

```sh
# .deb und .rpm (beim AppImage: ~/.local/share/rootmind/runtime/*/rootmind-backend)
/usr/lib/RootMind/backend/rootmind-backend service uninstall
sudo apt remove root-mind        # oder: sudo dnf remove root-mind
```

`service uninstall` hält die beiden Benutzerdienste an, entfernt sie und die
Programmkopie und behält Einstellungen, API-Keys, Index und MCP-Token in
`~/.local/share/rootmind`; `service uninstall --purge` löscht auch diese. Die
Chats liegen in `~/.local/share/io.github.cgaeking.rootmind`.

### Umstieg von 1.x

RootMind 1.x wurde pro Konto installiert und betrieb sein Backend in der App,
solange der Hintergrunddienst nicht eingeschaltet war. Der Installer von 2.0
entfernt eine 1.x-Installation deines Kontos von selbst. Ordner,
Einstellungen, API-Keys und Index des eingebauten Backends von 1.x werden
nicht übernommen: Füge deine Ordner und deinen API-Key erneut hinzu. War in
1.x der Hintergrunddienst eingeschaltet, wird er aktualisiert und behält
seine Ordner, Einstellungen und seinen Index. Die Chats bleiben in beiden
Fällen erhalten.

### Downloads prüfen

Jedes Release nennt in seinen Release Notes die Größe des Installers und
liefert `SHA256SUMS.txt` sowie eine Build-Provenance-Attestierung für jede
Datei. Der Hash muss mit der Zeile der Datei in `SHA256SUMS.txt`
übereinstimmen:

```powershell
(Get-FileHash .\RootMind-Setup-x64.exe -Algorithm SHA256).Hash.ToLower()
```

```sh
sha256sum --check --ignore-missing SHA256SUMS.txt
gh attestation verify RootMind-Setup-x64.exe --repo cgaeking/rootmind
```

### Aktualisieren und deinstallieren

Zum Aktualisieren startest du den Installer der neuen Version und bestätigst
die Abfrage. Er ersetzt die installierte Version an Ort und Stelle, ohne
weitere Rückfragen, und aktualisiert den Dienst; Ports, Tokens,
Einstellungen, Index und Chats bleiben erhalten. Läuft die App, bittet der
Installer darum, sie vorher zu schließen.

Deinstalliere RootMind über Windows-Einstellungen → Apps. Dabei werden die
RootMind-Dienste entfernt. Ihre Daten (Einstellungen, API-Keys, Index, Tokens
und Logs) bleiben in `%ProgramData%\RootMind`, außer du setzt den Haken bei
**Lösche die Anwendungsdaten** (englisch: **Delete the application data**);
dann werden sie und deine Chats ebenfalls gelöscht. Eine stille
Deinstallation (`/S`, wie Paketmanager sie ausführen) entfernt die Dienste
ebenfalls, löscht aber nie Daten. Um die Daten später zu löschen, entferne
`%ProgramData%\RootMind` in einer Eingabeaufforderung als Administrator.

### Der RootMind-Dienst

Das Backend von RootMind läuft als Windows-Dienst `RootMind-Backend`. Er hält
den Index aktuell, während die App geschlossen ist, und startet mit Windows;
die App ist ein Client dieses Dienstes. Einstellungen → **RootMind-Dienst**
zeigt seinen Zustand, bietet **Einrichten** an,
wenn der Dienst noch nicht eingerichtet ist (nach einer stillen oder
verwalteten Installation), und **Reparieren**, wenn er gestoppt ist oder nicht
antwortet (je eine Bestätigung als Administrator). Läuft der Dienst in einer älteren Version als die App,
bringt **Dienst aktualisieren** ihn auf die Version der App. Beides startet
das mit der App installierte Backend in `%ProgramFiles%\RootMind\backend`,
das nur Administratoren ändern können.

- Der Dienst gehört dem Konto, das RootMind installiert hat. Andere Konten
  auf demselben Computer können ihn nicht nutzen; um RootMind auf ein anderes
  Konto umzuziehen, deinstalliere es und installiere es von diesem Konto aus
  neu.
- Er läuft als LocalSystem, liest aber **nur Ordner und Dateien, die dein
  Konto ohne Administratorrechte öffnen kann**. Netzlaufwerke und Freigaben,
  Systemordner und die Profile anderer Benutzer stehen ihm nicht zur
  Verfügung, und die Ansicht **Ordner** markiert einen Ordner, den er nicht
  lesen kann.
- Seine REST-API verlangt ein Token. Das Token liegt in
  `%ProgramData%\RootMind\data\rest_token.txt`, das nur dein Konto, SYSTEM und
  Administratoren lesen können. Die App nutzt es erst, nachdem der Dienst auf
  dem Port bewiesen hat, dass er das Token kennt (ohne dass es gesendet wird).
- Ein zweiter Dienst, `RootMind-MCP-HTTP`, stellt MCP über HTTP auf 127.0.0.1
  mit einem eigenen Bearer-Token bereit (siehe
  [MCP-Clients verbinden](MCP.de.md#mcp-clients-verbinden)). Er ist standardmäßig
  eingeschaltet; der Haken bei **MCP über HTTP anbieten** schaltet ihn aus und
  wieder ein (eine Bestätigung als Administrator). Er steht unter
  Einstellungen → **MCP**, mit **Lokal**, dem Endpunkt für Programme auf diesem
  Computer mit dem MCP-Token, und dem Haken **Remote / Öffentlicher Zugang mit
  ngrok**, der eine öffentliche HTTPS-Adresse für Claude im Web und auf dem
  Handy und andere Cloud-Assistenten hinzufügt (siehe
  [Fernzugriff](MCP.de.md#fernzugriff)).
- Die Dienste laufen aus einer eigenen Kopie des Programms unter
  `%ProgramFiles%\RootMind Service\`, die nur Administratoren ändern können,
  und halten ihre Daten in `%ProgramData%\RootMind`.

**Unter Linux** ist derselbe Dienst das Paar systemd-Benutzerdienste deines
Kontos (siehe [Installation unter Linux](#installation-unter-linux)), und
nichts daran braucht root oder eine Bestätigung:

- Er läuft als dein Konto und liest, was dein Konto lesen kann, eingehängte
  Netzwerkfreigaben eingeschlossen. Andere Konten auf dem Computer bekommen
  eigene Dienste (auf anderen Ports), wenn sie RootMind starten.
- Sein REST-Token liegt in `~/.local/share/rootmind/data/rest_token.txt`, das
  nur dein Konto lesen kann (Modus 0600, in Ordnern mit Modus 0700). Die App
  nutzt es erst, nachdem der Dienst auf dem Port bewiesen hat, dass er das
  Token kennt.
- MCP über HTTP und der Fernzugriff funktionieren wie unter Windows;
  **MCP über HTTP anbieten** fügt `rootmind-mcp-http.service` ohne Rückfrage
  hinzu oder entfernt ihn.
- `systemctl --user status rootmind-backend` und
  `journalctl --user -u rootmind-backend` zeigen die Unit und ihr
  systemd-Protokoll; RootMinds eigene Logs liegen in
  `~/.local/share/rootmind/logs`.

Einstellungen → **RootMind-Dienst** wählt den Server der App: **Lokal** (dieser
Dienst, mit seiner Adresse) oder **Remote**, ein RootMind-Server unter einer
anderen Adresse mit optionalem Token (**Übernehmen**, **Prüfen**); die eigene
Adresse des Dienstes bleibt dort der Dienst.
[SECURITY.md](../SECURITY.md#background-service) (englisch) beschreibt das
Sicherheitsmodell.
