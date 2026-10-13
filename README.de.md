# RootMind

[English](README.md) | **Deutsch**

[![Neuestes Release](https://img.shields.io/github/v/release/cgaeking/rootmind)](https://github.com/cgaeking/rootmind/releases/latest)
[![backend CI](https://github.com/cgaeking/rootmind/actions/workflows/backend.yml/badge.svg?branch=main)](https://github.com/cgaeking/rootmind/actions/workflows/backend.yml?query=branch%3Amain)
[![desktop CI](https://github.com/cgaeking/rootmind/actions/workflows/desktop.yml/badge.svg?branch=main)](https://github.com/cgaeking/rootmind/actions/workflows/desktop.yml?query=branch%3Amain)
[![Lizenz: MIT](https://img.shields.io/github/license/cgaeking/rootmind)](LICENSE)
[![Plattform: Windows](https://img.shields.io/badge/platform-Windows%2010%20%7C%2011-0078D6)](#installation)
[![Downloads](https://img.shields.io/github/downloads/cgaeking/rootmind/total)](https://github.com/cgaeking/rootmind/releases)

> **Neu hier?** Der [Schnellstart](docs/QUICKSTART.de.md) führt in wenigen
> Schritten vom Download zum ersten Suchergebnis.

Lokale Dokumentensuche mit einer agentischen LLM-Schleife. RootMind indiziert
die Dateien in Ordnern deiner Wahl in SQLite FTS5. Wenn du eine Frage stellst,
entscheidet ein Sprachmodell, wonach gesucht wird und welche Dokumente es
liest; dazu nutzt es eine kleine Zahl lesender Tools. Die Antwort nennt ihre
Quellen, bis hinunter zu Seite, Folie oder Tabellenblatt, wo sich das
bestimmen lässt. Dieselben Tools stehen anderen Agenten über MCP zur
Verfügung.

Das Repository hat zwei Teile:

- **Backend** (Python, FastAPI): Indexer, Volltextsuche, die Agenten-Schleife,
  eine REST-API auf localhost und MCP-Server über stdio und HTTP.
- **Desktop-App** (Tauri 2 mit React und TypeScript, dazu eine Rust-Shell):
  eine Chat- und Suchoberfläche für Windows, auf Deutsch und Englisch, die das
  Backend mitbringt und startet.

> **Status:** Dies ist ein persönliches Projekt. Es wird nicht kommerziell
> gepflegt, und es gibt keine Support-Zusage. Issues und Pull Requests sind
> willkommen und werden nach bestem Bemühen beantwortet.

## Installation

Jedes [Release](https://github.com/cgaeking/rootmind/releases/latest) enthält
zwei Downloads für Windows (x64):

- **[RootMind-Setup-x64.exe](https://github.com/cgaeking/rootmind/releases/latest/download/RootMind-Setup-x64.exe)**,
  für die meisten, unter Windows 10 oder 11: die Desktop-App mit ihrem
  Backend. Starte die Datei; sie installiert RootMind für dein Konto, ohne
  Administratorrechte und ohne Python, und die App startet und beendet ihr
  Backend selbst.
- **[rootmind-backend-windows-x64.zip](https://github.com/cgaeking/rootmind/releases/latest/download/rootmind-backend-windows-x64.zip)**,
  für die Kommandozeile und Server: das Backend allein, ohne die App (siehe
  [Installation für Entwicklung und Server](#installation-für-entwicklung-und-server)).

Der Installer ist noch nicht codesigniert. Windows SmartScreen zeigt eventuell
„Der Computer wurde durch Windows geschützt“: Wähle **Weitere Informationen**
und dann **Trotzdem ausführen**. Ist Smart App Control aktiv, blockiert es den
Installer oder das Backend der App eventuell ohne diese Wahl; ein blockiertes
Backend meldet die App. Smart App Control kennt keine Ausnahme für eine
einzelne App; signierte Releases, die geplant sind, vermeiden das (siehe
[Code-Signing-Richtlinie](CODE_SIGNING.md)).

Füge danach unter **Ordner** einen Ordner hinzu. Die Indizierung beginnt
innerhalb etwa einer Minute, und die Suche funktioniert ohne API-Key, sobald
der Ordner indiziert ist. Um Fragen zu stellen, trage unter **Einstellungen**
einen API-Key für Anthropic, OpenAI oder xAI ein und speichere ihn. Optional
hält Einstellungen → **Hintergrunddienst** den Index aktuell, während die App
geschlossen ist (siehe [Hintergrunddienst](#hintergrunddienst)).

Die App folgt der Anzeigesprache von Windows (Deutsch oder Englisch);
Einstellungen → **Darstellung** → **Sprache** stellt sie um. Die Namen aus der
Oberfläche stehen in dieser README so, wie die deutsche Oberfläche sie zeigt.
Einstellungen → **Über RootMind** zeigt die Versionen von App und Backend und
verlinkt Releases und Issues.

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

Zum Aktualisieren startest du den Installer der neuen Version. Er ersetzt die
installierte Version an Ort und Stelle, im selben Ordner und ohne Rückfragen,
und behält deine Einstellungen, den Index und die Chats; läuft die App, bittet
er darum, sie vorher zu schließen. Ist der Hintergrunddienst eingeschaltet,
läuft er weiter, und die Einstellungen bieten dann **Dienst aktualisieren**
an (eine Bestätigung als Administrator).
MCP-Clients, die das eingebaute Backend über stdio nutzen (siehe
[MCP-Clients verbinden](#mcp-clients-verbinden)), werden ebenfalls beendet,
weil ihr Programm ersetzt wird; starte sie nach dem Update neu.

Deinstalliere RootMind über Windows-Einstellungen → Apps. Die Option **Lösche
die Anwendungsdaten** (englisch: **Delete the application data**) entfernt
auch deine Chats sowie Einstellungen, Index und Logs des eingebauten Backends.
Ist der Hintergrunddienst installiert, fragt der Deinstaller, ob er ihn
ebenfalls entfernen soll. Eine stille Deinstallation (`/S`, wie
Paketmanager sie ausführen) lässt den Dienst bestehen. Um ihn danach zu
entfernen, nutze eine PowerShell als Administrator:

```powershell
sc.exe qc RootMind-Backend      # BINARY_PATH_NAME ist das Programm des Dienstes
& "C:\Program Files\RootMind Service\<version>-<id>\rootmind-backend.exe" service uninstall
```

Die Daten des Dienstes bleiben in `%ProgramData%\RootMind` (ohne sein
REST-Token). Mit `--purge` werden sie ebenfalls gelöscht.

### Hintergrunddienst

Standardmäßig läuft das Backend nur, solange die App geöffnet ist.
Einstellungen → **Hintergrunddienst** macht daraus den Windows-Dienst
`RootMind-Backend`: Er
hält den Index aktuell, während die App geschlossen ist, und startet mit
Windows.

- Er wird Administratorkonten angeboten und braucht eine Bestätigung als
  Administrator. Beim Einschalten werden deine Ordner und Einstellungen
  übernommen; der Index wird für den Dienst neu aufgebaut.
- Der Dienst läuft als LocalSystem, liest aber **nur Ordner und Dateien, die
  dein Konto ohne Administratorrechte öffnen kann**. Netzlaufwerke und
  Freigaben, Systemordner und die Profile anderer Benutzer stehen ihm nicht
  zur Verfügung, und die Ansicht **Ordner** markiert einen Ordner, den er
  nicht lesen kann.
- Seine REST-API verlangt ein Token. Das Token liegt in
  `%ProgramData%\RootMind\data\rest_token.txt`, das nur dein Konto, SYSTEM und
  Administratoren lesen können. Die App nutzt es erst, nachdem sie geprüft
  hat, dass der Prozess des Dienstes selbst auf dem Port lauscht.
- Die Option **MCP zusätzlich über HTTP anbieten** fügt einen zweiten Dienst
  hinzu, `RootMind-MCP-HTTP`, für n8n
  und andere entfernte Agenten (siehe unten).

Beim Ausschalten des Dienstes gehen deine Einstellungen zurück an das
eingebaute Backend. [SECURITY.md](SECURITY.md#background-service) (englisch)
beschreibt das Sicherheitsmodell.

## MCP-Clients verbinden

MCP-Clients bekommen dieselben lesenden Tools wie der Agent der App und nutzen
ihr eigenes Modell.

**Eingebautes Backend (stdio).** Der MCP-Client startet das mitgelieferte
Backend mit `mcp` und verweist es auf den Datenordner der App. Ersetze `<you>`
durch deinen Windows-Benutzernamen (die Pfade sind die Standardwerte des
Installers). Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "rootmind": {
      "command": "C:\\Users\\<you>\\AppData\\Local\\RootMind\\backend\\rootmind-backend.exe",
      "args": ["mcp"],
      "env": {
        "RLM_HOME": "C:\\Users\\<you>\\AppData\\Local\\io.github.cgaeking.rootmind\\backend"
      }
    }
  }
}
```

Claude Code (eine Zeile; Schrägstriche funktionieren in jeder Shell):

```bash
claude mcp add --transport stdio rootmind --env RLM_HOME=C:/Users/<you>/AppData/Local/io.github.cgaeking.rootmind/backend -- C:/Users/<you>/AppData/Local/RootMind/backend/rootmind-backend.exe mcp
```

Der stdio-Server stellt nur bereit; der Index wird aktualisiert, solange die
App läuft. Er liest die Ordnerliste beim Start, also starte den MCP-Client
neu, nachdem du Ordner hinzugefügt oder entfernt hast.

MCP-Clients, die als MSIX-App verpackt sind, etwa Claude Desktop aus dem
Microsoft Store, führen ihre Server in einem App-Container aus: Windows legt
neue Dateien, die diese Prozesse unter AppData anlegen, in einer privaten
Kopie ab. Die gemeinsam genutzten Indexdateien von SQLite gehören dazu, daher
sieht ein solcher Client Index-Aktualisierungen eventuell nicht zuverlässig,
besonders wenn er den Server startet, während die App geschlossen ist. Mit
eingeschaltetem Hintergrunddienst umgeht MCP über HTTP (unten) das.

**Hintergrunddienst (HTTP).** Im Dienst-Modus nutzt du MCP über HTTP: Setze
unter Einstellungen → Hintergrunddienst den Haken bei **MCP über HTTP
anbieten**. Die Einstellungen zeigen dann den Endpunkt und das Token, jeweils
mit Kopier-Schaltfläche:

```bash
claude mcp add --transport http rootmind http://127.0.0.1:<port>/mcp \
  --header "Authorization: Bearer <token>"
```

Der Port ist 3000, wenn er frei ist, sonst einer aus 18400–18499. Das Token
bleibt über Updates hinweg erhalten, und der HTTP-Server übernimmt geänderte
Ordner ohne Neustart.

### MCP von n8n aus erreichen

Nutze in n8n den Knoten **MCP Client Tool** mit den Werten, die Einstellungen →
Hintergrunddienst anzeigt und kopiert:

| Feld | Wert |
|---|---|
| Endpoint | `http://127.0.0.1:<port>/mcp` |
| Server Transport | HTTP Streamable |
| Authentication | Header Auth |
| Header Name | `Authorization` |
| Header Value | `Bearer <token>` |

- **n8n auf demselben Rechner:** Nutze den Endpunkt wie angezeigt.
- **n8n in Docker auf demselben Rechner:** In einem Container ist `127.0.0.1`
  der Container selbst. Nutze `http://host.docker.internal:<port>/mcp`.
  Docker Desktop leitet diesen Namen an den Host weiter. Wird die Verbindung
  abgelehnt, betreibe auf dem Host einen Reverse Proxy, der auf einer Adresse
  lauscht, die der Container erreicht, und an `127.0.0.1:<port>` weiterleitet,
  und halte diese Adresse für andere Rechner geschlossen.
- **n8n auf einem anderen Rechner:** nur über einen Reverse Proxy mit
  TLS-Terminierung oder einen Tunnel vor `127.0.0.1:<port>`. Gib den
  unverschlüsselten HTTP-Port nie frei: Der Server spricht reines HTTP, und
  wer das Token hat, kann jedes indizierte Dokument lesen.

## So funktioniert es

```mermaid
flowchart LR
    desktop["Desktop-App<br/>Tauri + React"]
    mcpclient["MCP-Client<br/>z. B. Claude Desktop, Claude Code, n8n"]

    subgraph backend["Backend (Python)"]
        rest["REST-API<br/>127.0.0.1 + Bearer-Token"]
        mcp["MCP-Server<br/>stdio, HTTP + Bearer-Token"]
        engine["Agenten-Schleife<br/>rlm_engine/engine.py"]
        tools["Tool-Schicht<br/>tools.py"]
        repo[("SQLite<br/>FTS5 + Trigramme")]
        indexer["Indexer + Scheduler"]
    end

    folders[("Deine Ordner")]
    llm(["LLM-Anbieter<br/>Anthropic, OpenAI oder xAI"])

    desktop -- "/query, /search, /config" --> rest
    mcpclient --> mcp
    rest --> engine
    rest -- "Suche, Dokumente" --> repo
    engine <--> llm
    engine --> tools
    mcp --> tools
    tools --> repo
    indexer -- "liest Dateien" --> folders
    indexer -- "extrahierter Text" --> repo
```

1. **Die Indizierung nutzt kein LLM.** Ein Connector durchläuft jeden
   konfigurierten Ordner. Parser extrahieren Text aus PDF, DOCX, XLSX, PPTX,
   CSV, Markdown und anderen Textdateien. Der Text landet in SQLite: in einer
   Dokumenttabelle, einem FTS5-Index und einer Trigramm-Tabelle für
   tippfehlertolerante Suche. PDF-Seiten, Folien und Tabellenblätter behalten
   Markierungen im Text, sodass sich ein Treffer mit seiner Fundstelle
   zitieren lässt. Unveränderte Dateien werden anhand ihres Inhalts-Hashs
   übersprungen.
2. **Das Modell wählt, was es liest.** Für `POST /query` bietet die Engine dem
   Modell fünf lesende Tools an: `search_documents`, `search_fuzzy`,
   `list_documents`, `read_document` und `get_statistics`. Ein typischer
   Ablauf: suchen, den Bereich um einen Treffer lesen, eventuell mit anderen
   Wörtern erneut suchen, dann antworten.
3. **Die Schleife ist begrenzt.** Die Schleife macht höchstens 10
   Modellaufrufe, die Tools anfordern dürfen. Sucht das Modell danach noch,
   bittet ein letzter Aufruf mit abgeschalteten Tools um eine Antwort aus dem
   bisher Gefundenen. Pro Modell-Zug laufen höchstens 8 Tool-Aufrufe, und die
   Tool-Ergebnisse einer Frage sind auf 200.000 Zeichen begrenzt; Aufrufe
   darüber hinaus bekommen ein Fehlerergebnis, statt zu laufen. Jeder
   LLM-Aufruf hat ein Timeout, ein Lesevorgang liefert höchstens 50.000
   Zeichen, Tool-Fehler gehen als Fehlerergebnis an das Modell zurück, statt
   die Schleife zu beenden, und eine Antwort, die am Ausgabe-Token-Limit
   abgeschnitten wurde, sagt das.
4. **Antworten nennen ihre Quellen.** Die Antwort listet die Dokumente, die das
   Modell gelesen hat, mit Seite, Folie oder Tabellenblatt, wo sich das
   bestimmen lässt, jeden Tool-Aufruf und warum jeder LLM-Aufruf geendet hat.
5. **MCP-Clients bringen ihr eigenes Modell mit.** Über MCP stellt RootMind
   dieselben Tool-Definitionen bereit, und das Modell des Clients entscheidet.
   Suche und Indizierung brauchen nie einen API-Key; nur Fragen
   (`POST /query`), Chat-Titel und die Modell-Liste in den Einstellungen rufen
   den Anbieter auf.

Die Desktop-App betreibt das Backend auf eine von drei Arten: das
**eingebaute** Backend (ein Kindprozess der App, der Standard), der
**Hintergrunddienst** oder ein **eigenes** Backend unter einer Adresse, die du
unter Einstellungen → **Erweitert** einträgst.
[ARCHITECTURE.md](ARCHITECTURE.md#deployment-modes) (englisch) beschreibt die
Details.

## Warum eine Tool-Schleife statt Embedding-RAG

Eine typische RAG-Pipeline zerlegt Dokumente in Abschnitte (Chunks), bettet sie
ein, holt die ähnlichsten Treffer per Vektorähnlichkeit und fügt diese
Abschnitte in den Prompt ein. Das Modell sieht nur, was dieser Abrufschritt
ausgewählt hat.

RootMind hält den vollständigen Text jedes Dokuments in einem Volltextindex
und lässt das Modell die Suche steuern. Es kann suchen, sich die Snippets
ansehen, den Text um einen Treffer lesen und mit anderen Begriffen erneut
suchen, wenn die erste Anfrage danebenliegt. Das wird manchmal RLM-artige oder
agentische Suche genannt. Das Engine-Modul heißt nach dieser Idee
`rlm_engine`; in dieser Codebasis ist es eine begrenzte Tool-Schleife, kein
Modell, das sich rekursiv selbst aufruft.

| Funktioniert gut | Funktioniert weniger gut |
|---|---|
| Exakte Begriffe: Rechnungsnummern, Produktcodes, Namen, Beträge | Synonyme und Umschreibungen: „Urlaub“ findet „bezahlte Freistellung“ nur, wenn das Modell das andere Wort versucht |
| Kein Embedding-Modell und kein Vektorspeicher; die Indizierung parst nur Text und funktioniert offline | Fragen in einer anderen Sprache als die Dokumente |
| Das Modell liest den Kontext um einen Treffer, etwa den Tabellenkopf über einer Zahl, statt eines isolierten Abschnitts | Mehr LLM-Runden pro Frage, also mehr Latenz und Token-Kosten |
| Jeder Suchschritt ist als Tool-Aufruf sichtbar | Die Antwortqualität hängt von der Suchstrategie des Modells ab |

Die Trigramm-Fuzzy-Suche deckt Tippfehler ab, keine Synonyme. Der Eval-Fall
c09 (die Synonym-Falle) hält diese Schwäche sichtbar.

## Installation für Entwicklung und Server

**Mit uv** (jedes Betriebssystem mit Python 3.12 oder neuer; uv installiert
es bei Bedarf):

```bash
uv tool install "git+https://github.com/cgaeking/rootmind@v1.2.0#subdirectory=backend"
rootmind --help        # api, index, rebuild-trigrams, mcp, mcp-http, service
```

Das installiert die Befehle `rootmind`, `rootmind-mcp` und
`rootmind-mcp-http`. Unter Windows funktionieren `rootmind service status` und
`rootmind service uninstall` aus einer solchen Installation, aber
`rootmind service install` wird abgelehnt und gibt die Download-Adresse des
Release-Builds aus. Der Dienst läuft als SYSTEM und darf deshalb nur ein
Programm aus einem Ordner ausführen, den allein Administratoren ändern können;
`service install` kopiert den Release-Build in einen solchen Ordner, während
eine uv- oder pip-Umgebung in einem Ordner liegt, den dein Konto ändern kann.

**Windows-Server oder ein Rechner ohne die App.** Das Release enthält auch das
Backend allein, `rootmind-backend-windows-x64.zip`. Lade es in PowerShell
herunter und prüfe es:

```powershell
$base = "https://github.com/cgaeking/rootmind/releases/latest/download"
Invoke-WebRequest "$base/rootmind-backend-windows-x64.zip" -OutFile rootmind-backend-windows-x64.zip -UseBasicParsing
Invoke-WebRequest "$base/SHA256SUMS.txt" -OutFile SHA256SUMS.txt -UseBasicParsing
$line = Select-String -Path SHA256SUMS.txt -SimpleMatch "rootmind-backend-windows-x64.zip"
$expected = ($line.Line -split '\s+')[0]
$actual = (Get-FileHash rootmind-backend-windows-x64.zip -Algorithm SHA256).Hash.ToLower()
if ($actual -ne $expected) { throw "Checksum mismatch: $actual" }
Expand-Archive rootmind-backend-windows-x64.zip -DestinationPath .
```

Dann, aus einer PowerShell als Administrator des Kontos, dessen Ordner
RootMind indizieren soll (es muss ein lokaler Administrator sein), in dem
Ordner, in den du entpackt hast:

```powershell
Set-Location <download folder>   # der Ordner, der rootmind-backend enthält
# installieren oder einen installierten Dienst aktualisieren
.\rootmind-backend\rootmind-backend.exe service install --import-config C:\path\to\config.yaml
# Zustand, Port, Version und Token-Dateien (funktioniert ohne Administratorrechte)
.\rootmind-backend\rootmind-backend.exe service status
# den Dienst entfernen; --purge löscht auch seine Daten
.\rootmind-backend\rootmind-backend.exe service uninstall
```

`service install` kopiert das Programm nach
`%ProgramFiles%\RootMind Service\<version>-<id>\`, der entpackte Ordner wird
danach also nicht mehr gebraucht. Der Dienst nutzt Port 8000 und mit
`--mcp-http` Port 3000 für MCP; `--port auto` und `--mcp-port auto` weichen
auf 18300–18399 und 18400–18499 aus, wenn diese belegt sind.
`--import-config` übernimmt die Ordner und Einstellungen aus einer
`config.yaml` wie
[`backend/config.example.yaml`](backend/config.example.yaml). Ein erneutes
`service install` aktualisiert den Dienst und behält seine Ports, Tokens und
Daten; `service install --help` listet alle Optionen. Das REST-Token liegt in
`%ProgramData%\RootMind\data\rest_token.txt` und das MCP-Token in
`%ProgramData%\RootMind\data\api_token.txt`.

Das Zip läuft auch ohne Dienst, zum Beispiel
`rootmind-backend.exe api --require-token` oder
`rootmind-backend.exe mcp-http`.

## Aus dem Quellcode starten

Du brauchst [uv](https://docs.astral.sh/uv/); es installiert bei Bedarf
Python 3.12. Die Desktop-App braucht außerdem Node 24, pnpm 11, Rust 1.96
oder neuer und unter Windows die MSVC Build Tools und WebView2.

### 1. Indizieren und suchen ohne API-Key

```bash
cd backend
uv sync
cp config.example.yaml config.yaml   # indiziert den fiktiven Demo-Korpus in evals/corpus
uv run rootmind index                # baut data/index.db; kein LLM beteiligt
uv run rootmind api                  # REST-API auf http://127.0.0.1:8000, ohne Token
```

In einem zweiten Terminal:

```bash
curl "http://127.0.0.1:8000/search?q=total%20due&limit=3&snippets=true"
```

Der erste Treffer ist `invoice_INV-2026-0142.pdf` mit `"page": "Page 2"` und
einem Snippet um die Fundstelle. Ohne `snippets=true` liefert die Suche nur
die sortierte Liste, was bei großen Indizes schneller ist.

Die interaktive API-Dokumentation liegt unter <http://127.0.0.1:8000/docs>.
Solange die API läuft, indiziert sie etwa 30 Sekunden nach dem Start neu und
danach alle 24 Stunden (`indexer.sync_interval_hours`). Um eigene Dateien zu
indizieren, füge in `config.yaml` Einträge unter `connectors` hinzu. Nutze
absolute Pfade: Relative Connector-Pfade werden relativ zum
Arbeitsverzeichnis aufgelöst.

`rootmind api`, so gestartet, verlangt kein Token. Um eines zu verlangen,
setze `RLM_API_TOKEN` oder übergib `--require-token`, was
`data/rest_token.txt` anlegt, und sende `Authorization: Bearer <token>` mit
jeder Anfrage. Die Dokumentationsseiten sind abgeschaltet, solange ein Token
verlangt wird.

### 2. Eine Frage stellen

Setze einen Key für einen Anbieter (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY` oder
`XAI_API_KEY`) in der Umgebung oder in `backend/.env` (siehe
[`.env.example`](backend/.env.example)). Wähle Anbieter und Modell unter `llm`
in `config.yaml`, starte die API neu und dann:

```bash
curl -X POST http://127.0.0.1:8000/query \
  -H "Content-Type: application/json" \
  -d '{"question": "What is the total amount due on invoice INV-2026-0142?"}'
```

Die Antwort enthält `answer`, `sources` (mit `page`, wo bekannt),
`tool_calls` und `tokens_used`. Die Frage, Such-Snippets und
Dokumentauszüge gehen an den konfigurierten Anbieter; siehe
[SECURITY.md](SECURITY.md) (englisch).

### 3. Die Tools aus einem MCP-Client nutzen

Die MCP-Server lesen dieselbe `config.yaml` und Datenbank wie die REST-API.
Sie stellen nur bereit und indizieren nie; halte den Index also mit
`rootmind index` oder einer laufenden REST-API aktuell.

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "rootmind": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/rootmind/backend", "run", "rootmind-mcp"]
    }
  }
}
```

Findet Claude Desktop `uv` nicht, trage den absoluten Pfad der
`uv`-Programmdatei unter `command` ein. Unter Windows schreibst du die Pfade
mit Schrägstrichen oder mit maskierten Backslashes (`C:\\...`).

Claude Code:

```bash
claude mcp add --transport stdio rootmind -- \
  uv --directory /absolute/path/to/rootmind/backend run rootmind-mcp
```

Über HTTP, für MCP-Clients, die keinen lokalen Prozess starten können:

```bash
cd backend
uv run rootmind-mcp-http             # http://127.0.0.1:3000/mcp
```

Beim ersten Start erzeugt der Server ein Token und speichert es in
`data/api_token.txt` (unter `RLM_HOME` oder, wenn das nicht gesetzt ist, im
Arbeitsverzeichnis); alternativ setzt du `MCP_API_TOKEN`. Der Server gibt aus,
woher das Token stammt, und einen kurzen Fingerabdruck davon, nie das Token
selbst. Dann:

```bash
claude mcp add --transport http rootmind http://127.0.0.1:3000/mcp \
  --header "Authorization: Bearer <token from data/api_token.txt>"
```

Der stdio-Server liest die Ordnerliste beim Start; starte ihn (den
MCP-Client) also neu, nachdem du Ordner hinzugefügt oder entfernt hast. Der
HTTP-Server liest sie neu ein, wenn sich `config.yaml` ändert. Der
HTTP-Server bindet an 127.0.0.1 und spricht reines HTTP; um ihn von einem
anderen Rechner aus zu erreichen, stelle einen Proxy mit TLS-Terminierung
oder einen Tunnel davor und gib den unverschlüsselten Port nie frei.
`rootmind-mcp-http --index` startet in diesem Prozess zusätzlich den
Hintergrund-Indexer. Nutze das nur, wenn keine REST-API dieselbe Datenbank
indiziert: Jeder indizierende Prozess entfernt die Dokumente von Ordnern, die
in seiner eigenen Kopie der Konfiguration fehlen.

### 4. Die Desktop-App starten (Entwicklung)

```bash
# Terminal 1: Backend, mit dem Vite-Dev-Origin in CORS erlaubt
cd backend
RLM_DEV=1 uv run rootmind api        # PowerShell: $env:RLM_DEV=1; uv run rootmind api

# Terminal 2: Desktop
cd desktop
pnpm install
pnpm tauri dev
```

Ein Entwicklungs-Build hat kein mitgeliefertes Backend, daher fragt die App
nach einer Backend-Adresse: Bestätige `http://127.0.0.1:8000`. Mehr dazu in
[desktop/README.md](desktop/README.md) (englisch).

### Den Installer selbst bauen

Unter Windows, im Wurzelverzeichnis des Repositorys:

```powershell
# 1. Backend: PyInstaller-onedir-Build -> backend/dist/rootmind-backend/
cd backend
uv sync --locked --no-dev --group build
uv run --no-sync pyinstaller packaging/rootmind-backend.spec --noconfirm --clean --distpath dist
cd ..

# 2. Nach desktop/src-tauri/binaries/rootmind-backend/ kopieren und prüfen
python scripts/prepare_backend.py

# 3. Installer mit dem Release-Overlay, das das Backend hinzufügt
cd desktop
pnpm install
pnpm tauri build --config src-tauri/tauri.release.conf.json
```

Der Installer landet in `desktop/src-tauri/target/release/bundle/nsis/`.
Führe danach `uv sync` in `backend/` aus, um die Entwicklungswerkzeuge
zurückzubekommen. Ohne das Overlay baut `pnpm tauri build` eine App ohne
Backend.

## Aufbau des Repositorys

```
backend/
  src/rootmind/
    rlm_engine/engine.py   Agenten-Schleife
    tools.py               Tool-Registry, Argumentprüfung, Ergebnisformatierung
    mcp_stdio.py           MCP-Server über stdio
    mcp_http.py            MCP-Server über HTTP mit Bearer-Token
    api/                   REST-API (FastAPI)
    auth.py, access.py     REST-Token; Schnittstelle der Ordner-Zugriffsregeln
    winaccess.py           Ordner-Zugriffsregeln des Windows-Dienstes (AuthZ)
    service/               Windows-Dienst: install, uninstall, status, Dienst-Host
    database/              SQLite-Modelle, FTS5- und Trigramm-Suche
    indexer/               Parser, Indexer, Sync-Scheduler
    connectors/            Connector für lokale Ordner
  tests/                   Offline-Testsuite mit Fake-LLM-Clients; e2e/ Dienst-Test
  evals/                   fiktiver Korpus und Eval-Fälle
  scripts/                 Eval-Runner und Korpus-Generator
  packaging/               PyInstaller-Spec, Smoke-Test, Tree-Hash
  deploy/windows/          run-backend.bat: die REST-API aus dem Quellcode in einem Konsolenfenster
desktop/
  src/                     React- und TypeScript-Oberfläche
  src-tauri/               Rust-Shell: eingebautes Backend, Dienst-Befehle, Chat-Verlauf;
                           installer/ mit NSIS-Vorlage und Hooks
  scripts/                 Smoke-Test des Installers
packaging/winget/          Vorlagen für winget-Manifeste
scripts/                   Versionsprüfung, Release Notes, Backend-Vorbereitung, winget-Rendering,
                           Aktualisierung der NSIS-Vorlage
.github/workflows/         CI und Release
```

Designentscheidungen, Schutzmechanismen und Abwägungen beschreibt
[ARCHITECTURE.md](ARCHITECTURE.md), das Bedrohungsmodell
[SECURITY.md](SECURITY.md) und die Änderungen je Version
[CHANGELOG.md](CHANGELOG.md) (alle englisch).

## Tests und Evals

Beide laufen offline und brauchen keinen API-Key:

```bash
cd backend
uv sync
uv run pytest -q                      # Fake-LLM-Clients, generierter Test-Korpus
uv run python scripts/run_evals.py    # Such-Regressionstest: geskriptete Policy, kein Modell
```

**Tests.** Eine Autouse-Fixture ersetzt die Konstruktoren der Anthropic- und
OpenAI-Clients durch Funktionen, die eine Ausnahme werfen, und entfernt
API-Keys aus der Umgebung. Tests injizieren geskriptete Fake-Clients
([`tests/fakes.py`](backend/tests/fakes.py)) über den Parameter `client` der
Engine. Jeder Test bekommt ein eigenes `RLM_HOME` und Arbeitsverzeichnis.
Tests, die Dokumente brauchen, bekommen einen kleinen generierten Korpus mit
je einer PDF-, DOCX-, XLSX-, PPTX-, CSV-, Markdown- und Textdatei. Tests der
Windows-Ordner-Zugriffsregeln und der Ordnerrechte des Dienstes laufen nur
unter Windows; echte Dienste installiert erst der End-to-End-Test in der CI.

**Evals.** [`scripts/run_evals.py`](backend/scripts/run_evals.py) indiziert den
fiktiven Korpus in [`backend/evals/corpus/`](backend/evals/corpus/) mit dem
echten Indexer und schickt 16 Fragen durch die echte Engine.

- Der *Live-Modus* ist das eigentliche Eval: Er lässt ein echtes Modell
  mehrmals pro Fall laufen und meldet die Erfolgsquote (erwartete Quelle
  zitiert, geforderte Zeichenketten vorhanden, verbotene abwesend), die
  Varianz der Tool-Pfade, Tool-Aufrufe, Tokens, Latenz und Stop-Gründe. Er
  liest Keys nur aus Umgebungsvariablen und läuft nie in der CI.
- Der *Dry-Modus* ist ein Such-Regressionstest, kein Eval des Agenten: Eine
  geskriptete Policy ersetzt das Modell, sucht mit einer für jeden Fall von
  Hand geschriebenen Anfrage (`dry_search` im Datensatz), liest das
  vorgeschlagene Fenster und antwortet mit dem Auszug. Er prüft, dass Index
  und Tool-Schicht das richtige Dokument weiterhin an erster Stelle (hit@1)
  und unter den ersten fünf Treffern (hit@5) liefern und die richtige Seite,
  Folie oder das richtige Tabellenblatt zitieren. Mit handverlesenen Anfragen
  ist 1.0 auf den 15 bewerteten Fällen das erwartete Ergebnis; c09, die
  Synonym-Falle, ist der bekannte Fehlgriff. Die CI schlägt fehl, wenn hit@5
  unter 1.0 fällt. Über die Antwortqualität sagt er nichts.

Details: [backend/evals/README.md](backend/evals/README.md) (englisch).

**CI.** GitHub-Actions-Workflows mit per Commit-SHA gepinnten Actions und ohne
Secrets. Nur der Release-Job darf in das Repository schreiben:

| Workflow | Was er ausführt |
|---|---|
| [`backend.yml`](.github/workflows/backend.yml) | ruff check und Format-Prüfung; die Versionsprüfung über Backend und Desktop; die Tests der Release-Skripte unter Ubuntu und Windows; pytest mit Coverage unter Ubuntu (Python 3.12 und 3.14) und Windows (Python 3.12); den Such-Regressionstest im Dry-Modus |
| [`backend-exe.yml`](.github/workflows/backend-exe.yml) | Den PyInstaller-onedir-Build unter Windows, einen Smoke-Test der Programmdatei (REST, MCP über HTTP und stdio, Indizierung jedes Dateityps) und ihren Tree-Hash; danach einen End-to-End-Test des Windows-Dienstes auf dem Runner mit Administratorrechten: Installation, Berechtigungen, Tokens, Lese-Beschränkung, Update und Deinstallation. Bei Pull Requests läuft der Dienst-Test nur, wenn sich dienstbezogener Code geändert hat |
| [`desktop.yml`](.github/workflows/desktop.yml) | ESLint, TypeScript-Typprüfung, vitest, Vite-Build, `cargo fmt`, `cargo clippy`, `cargo check`, `cargo test` unter Windows |
| [`release.yml`](.github/workflows/release.yml) | Für einen Versions-Tag `v*`: das Backend über `backend-exe.yml`; den Installer mit diesem Backend, eine Prüfung, dass er eine installierte Version ohne Neuinstallations-Seite an Ort und Stelle aktualisiert, und einen Smoke-Test für Installation, Update und Deinstallation; die winget-Manifeste, gegen ihre Schemas geprüft; dann ein GitHub-Release mit Installer, Backend-Zip, `SHA256SUMS.txt` und Attestierungen. Manuelle Läufe und Pull Requests, die das Packaging ändern, sind Probeläufe, die nichts veröffentlichen |
| [`secrets.yml`](.github/workflows/secrets.yml) | detect-secrets über alle versionierten Dateien, gegen die geprüfte Baseline `.secrets.baseline` |

Dependabot ([`.github/dependabot.yml`](.github/dependabot.yml)) schlägt
monatlich Abhängigkeits-Updates für alle vier Ökosysteme vor.

## Sicherheitsmodell in Kürze

- RootMind ist für eine Person auf ihrem eigenen Rechner gebaut. Die REST-API
  bindet an 127.0.0.1, lehnt Verbindungen von anderen Rechnern ab, weist
  fremde `Host`-Header zurück und erlaubt Cross-Origin-Anfragen nur von der
  Desktop-App.
- Das eingebaute Backend der App und der Hintergrunddienst verlangen auf der
  REST-API ein Bearer-Token. `rootmind api` aus dem Quellcode läuft ohne
  Token, solange du keines verlangst; ohne Token kann jeder lokale Prozess sie
  aufrufen.
- Der Hintergrunddienst läuft als LocalSystem, liest aber nur, was das
  installierende Konto ohne Administratorrechte lesen kann. Er wird nur
  Administratorkonten angeboten.
- Der MCP-HTTP-Server verlangt ein Bearer-Token und bindet an 127.0.0.1.
- Die Indizierung bleibt auf deinem Rechner. Beim Beantworten einer Frage
  gehen die Frage, Such-Snippets und Dokumentauszüge an den konfigurierten
  LLM-Anbieter.
- Indizierte Dokumente sind nicht vertrauenswürdige Eingaben. Alle Tools sind
  rein lesend, Dokumenttext ist als Daten gekennzeichnet, und die Tool-Nutzung
  einer Frage ist begrenzt, aber Prompt Injection ist kein gelöstes Problem.

Das vollständige Bedrohungsmodell steht in [SECURITY.md](SECURITY.md)
(englisch).

## Einschränkungen

- Die Desktop-App, ihren Installer und den Hintergrunddienst gibt es nur für
  Windows. Das Backend ist plattformunabhängiges Python; die CI führt seine
  Tests unter Ubuntu und Windows aus.
- Die Release-Binärdateien sind noch nicht codesigniert (siehe
  [Installation](#installation)). RootMind ist noch nicht im
  winget-Repository; die Einreichung ist geplant, sobald die Binärdateien
  signiert sind.
- Keine Texterkennung (OCR): Gescannte PDFs ohne Textebene liefern keinen
  Text.
- Alte `.doc`- und `.xls`-Dateien gehen an die DOCX- und XLSX-Parser und
  lassen sich meist nicht parsen.
- Kein Streaming: Die Desktop-App wartet auf die vollständige Antwort.
- Die Volltextsuche kennt keine Synonyme und keinen sprachübergreifenden
  Abgleich (siehe die Tabelle der Abwägungen oben).
- Das eingebaute Backend und der Hintergrunddienst haben jeweils einen eigenen
  Index. Der Dienst baut seinen Index von Grund auf neu, und Quellen in
  älteren Chats öffnen sich eventuell erst, wenn er aufgeholt hat.
- Der Hintergrunddienst kann keine Netzlaufwerke oder Freigaben indizieren.
- Pro Datenbank sollte nur ein Prozess indizieren. Die MCP-Server stellen
  standardmäßig nur bereit; setze `RLM_DISABLE_SCHEDULER=1` bei jeder
  weiteren REST-API, die dieselbe Datenbank nutzt.
- Der MCP-stdio-Server liest die Ordnerliste beim Start; starte ihn nach
  Änderungen an den Ordnern neu.
- Eine inkrementelle Synchronisierung berechnet den Hash jeder Datei in jedem
  konfigurierten Ordner, was bei sehr großen Ordnern langsam ist.

## Mitwirken

[CONTRIBUTING.md](CONTRIBUTING.md) (englisch) beschreibt die
Entwicklungsumgebung, Prüfungen, Konventionen und die Release-Schritte. Melde
Sicherheitsprobleme bitte vertraulich, wie in [SECURITY.md](SECURITY.md)
beschrieben.

## Lizenz

[MIT](LICENSE), Copyright (c) 2026 Christian Gäking.

Das gepackte Backend und der Installer enthalten Abhängigkeiten von Dritten,
die ihre eigenen Lizenzen behalten.

## Code-Signing-Richtlinie

Free code signing provided by [SignPath.io](https://signpath.io), certificate
by [SignPath Foundation](https://signpath.org). Was signiert wird, die Rollen im
Team und die Datenschutzerklärung stehen in [CODE_SIGNING.md](CODE_SIGNING.md)
(englisch).
