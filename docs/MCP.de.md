# MCP-Clients im Detail

[English](MCP.md) | **Deutsch**

Die [README](../README.de.md#rootmind-in-claude-und-anderen-programmen-nutzen) hat die Kurzfassung. Hier stehen die Details: Endpunkt und Token, wie die Erweiterung für Claude Desktop arbeitet, n8n in Docker, Fernzugriff und eigener Tunnel.

## MCP-Clients verbinden

MCP-Clients bekommen dieselben lesenden Tools wie der Agent der App und nutzen
ihr eigenes Modell. Verbinde sie mit MCP über HTTP des Dienstes:
Einstellungen → **MCP** zeigt den **MCP-Endpunkt** und kopiert ihn
und den MCP-Token (**MCP-Token kopieren**). Für Claude Desktop gibt es ein Bundle,
das beides nicht braucht (siehe
[RootMind in Claude Desktop nutzen](#rootmind-in-claude-desktop-nutzen)).
Claude Code:

```bash
claude mcp add --transport http rootmind http://127.0.0.1:<port>/mcp \
  --header "Authorization: Bearer <token>"
```

Der Port ist 3000, wenn er frei ist, sonst einer aus 18400–18499. Das Token
bleibt über Updates hinweg erhalten, und der Server übernimmt geänderte
Ordner ohne Neustart.

### RootMind in Claude Desktop nutzen

1. Lade
   [rootmind.mcpb](https://github.com/cgaeking/rootmind/releases/latest/download/rootmind.mcpb)
   aus dem [neuesten Release](https://github.com/cgaeking/rootmind/releases/latest)
   herunter.
2. Doppelklicke die Datei. Claude Desktop öffnet sie, zeigt die Tools von
   RootMind und installiert sie, sobald du bestätigst.

Einzurichten ist nichts. Die Erweiterung findet den RootMind-Dienst auf
diesem Computer, liest dort Port und Token für MCP und verbindet sich mit MCP
über HTTP auf 127.0.0.1. Sie braucht ein installiertes RootMind mit
eingeschaltetem **MCP über HTTP anbieten** (Standard; Einstellungen →
RootMind-Dienst), und Claude Desktop muss unter dem Konto laufen, das RootMind
nutzt, denn nur dieses Konto kann das Token lesen. Unter Linux findet sie den
Dienst über `~/.local/share/rootmind/service.json`, das die Programmkopie der
Dienste nennt (kein Geheimnis). Fehlt
etwas, sagt die Fehlermeldung der Erweiterung, was zu tun ist, etwa RootMind
zu installieren oder MCP über HTTP einzuschalten; danach funktioniert die
nächste Anfrage, ohne die Erweiterung neu zu installieren. Nach einer
Reparatur folgt sie dem Dienst selbst auf einen neuen Port. Zum Aktualisieren
installierst du die `rootmind.mcpb` eines neueren Releases. Das Bundle ist
noch nicht signiert, deshalb kennzeichnet Claude Desktop es als nicht
signiert. Quelltext und Funktionsweise:
[packaging/mcpb](../packaging/mcpb/README.md) (englisch).

Das Bundle steht auch in der offiziellen
[MCP Registry](https://registry.modelcontextprotocol.io) als
`io.github.cgaeking/rootmind`. Jedes Release (kein Vorabrelease) aktualisiert
den Eintrag mit seiner Download-URL und SHA-256, sodass Clients, die aus der
Registry installieren, das aktuelle Bundle bekommen.

Claude Desktop startet nur lokale (stdio-)MCP-Server; die Erweiterung ist ein
solcher Server und leitet an den HTTP-Endpunkt des Dienstes weiter. Das
Programm im Ordner der App (`rootmind-backend.exe mcp`) kann für die
installierte App nicht als stdio-Server dienen: Es kann den Index des
Dienstes nicht öffnen.
Der stdio-Server ist für eine Installation aus dem Quellcode oder mit uv und
eigener `config.yaml` gedacht (siehe
[3. Die Tools aus einem MCP-Client nutzen](DEVELOPMENT.de.md#3-die-tools-aus-einem-mcp-client-nutzen)).

### MCP von n8n aus erreichen

Nutze in n8n den Knoten **MCP Client Tool** mit den Werten, die
Einstellungen → MCP anzeigt; **Kopieren** unter **n8n (MCP Client
Tool)** kopiert sie mitsamt Token:

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
  TLS-Terminierung oder einen Tunnel (etwa den [Fernzugriff](#fernzugriff))
  vor `127.0.0.1:<port>`. Gib den
  unverschlüsselten HTTP-Port nie frei: Der Server spricht reines HTTP, und
  wer das Token hat, kann jedes indizierte Dokument lesen. Siehe
  [RootMind aus Cloud-Assistenten erreichen](#rootmind-aus-cloud-assistenten-erreichen).

### RootMind aus Cloud-Assistenten erreichen

Assistenten, die auf fremden Servern laufen, etwa Grok Bot, ein in der Cloud
gehostetes n8n oder andere Web-Agenten, erreichen `127.0.0.1` auf deinem
Rechner nicht. Sie brauchen eine öffentliche HTTPS-Adresse, die an den
MCP-Endpunkt weiterleitet; die liefert ein Tunnel auf deinem Rechner: der
eingebaute **Fernzugriff** von RootMind (ngrok) oder ein Tunnel, den du selbst
betreibst (Tailscale Funnel, der ngrok-Agent). In beiden Fällen leitet der
Tunnel nur an MCP über HTTP weiter, und der Client schickt dasselbe Token.

#### Fernzugriff

RootMind kann den Tunnel selbst betreiben. Der MCP-Server des RootMind-Dienstes
(`RootMind-MCP-HTTP`) öffnet ihn mit dem quelloffenen SDK von ngrok; ein
ngrok-Programm musst du nicht installieren. Standardmäßig ist er aus.

1. Lege bei [ngrok.com](https://ngrok.com) ein kostenloses Konto an und
   kopiere den **ngrok Authtoken** aus dem Dashboard (**Getting Started ›
   Your Authtoken**). Lege unter **Domains** die kostenlose feste Domain an;
   ohne sie vergibt ngrok nach jedem Neustart eine neue Adresse.
   **Kostenlosen ngrok Authtoken und Domain holen** in der App führt dich
   durch diese Schritte.
2. Setze in Einstellungen → MCP den Haken bei **Remote / Öffentlicher Zugang
   mit ngrok**, füge
   den ngrok Authtoken und die Domain ein, wähle **Verbinden** und bestätige.
   Der ngrok Authtoken wird nie wieder angezeigt: Das Feld zeigt danach nur
   **Gespeichert** (**Ersetzen** und **Entfernen** ändern ihn). Den MCP-Token
   lehnt das Feld ab: Er gehört in den Client, nicht hierher.
3. Der Status wechselt zu **Verbunden** und zeigt den **Öffentlichen
   MCP-Endpunkt** (`https://<domain>/mcp`) mit **Kopieren** und **MCP-Token
   kopieren**. **RootMind in Claude im Web und auf dem Handy nutzen** zeigt
   die Werte für den Konnektor zum Kopieren; siehe
   [Im Assistenten](#im-assistenten).

Der Fernzugriff braucht **MCP über HTTP anbieten**; schaltest du das aus, ist
auch der Fernzugriff aus. Änderungen brauchen keine Bestätigung als
Administrator, wirken nach wenigen Sekunden und überstehen Neustarts und
Updates: Der Tunnel startet mit dem Dienst. Entfernst du den Haken bei
**Remote**, ist die öffentliche Adresse sofort geschlossen. Bricht die Verbindung zu ngrok ab,
verbindet sich der Dienst neu; eine abgelehnte Verbindung (ein falscher
Authtoken, eine Domain, die nicht dir gehört oder schon belegt ist) erscheint
mit der Meldung von ngrok im Status und wird mit wachsenden Pausen (bis zu
fünf Minuten) erneut versucht.

- ngrok erlaubt einen Endpunkt pro Domain. Betreibst du schon einen eigenen
  ngrok-Tunnel auf derselben Domain, beende ihn, bevor du den Fernzugriff
  einschaltest.
- Der Authtoken liegt in `%ProgramData%\RootMind\data\ngrok_authtoken.txt`,
  das nur SYSTEM, Administratoren und das Konto des MCP-Dienstes lesen können;
  er steht nie in `config.yaml`, wird von der API der App nie zurückgegeben
  und nie protokolliert. Eine Deinstallation ohne **Lösche die
  Anwendungsdaten** behält ihn.
- Kostenlose ngrok-Konten haben Volumengrenzen; siehe die Preisseite von ngrok.

#### Eigener Tunnel

**Mit dem ngrok-Agent** (kostenloses Konto, eine feste kostenlose Domain):

1. Lege bei [ngrok.com](https://ngrok.com) ein Konto an, installiere den Agent
   und hinterlege deinen Authtoken:

   ```powershell
   winget install ngrok.ngrok
   ngrok config add-authtoken <dein-ngrok-authtoken>
   ```

2. Notiere im ngrok-Dashboard unter **Domains** deine kostenlose Domain und
   starte den Tunnel zum MCP-Port (3000, außer die Einstellungen zeigen einen
   anderen):

   ```powershell
   ngrok http 3000 --url https://<deine-kostenlose-domain>
   ```

   Damit er nach einem Neustart weiterläuft, lege den Tunnel in der
   Konfigurationsdatei von ngrok an und installiere ihn als Dienst
   (`ngrok service install`; siehe die Doku von ngrok).

**Mit Tailscale Funnel** (kostenloses Tailscale-Konto und App):

```powershell
tailscale funnel --bg 3000
```

Der Befehl zeigt die öffentliche Adresse (`https://<rechner>.<tailnet>.ts.net`)
und lässt den Funnel im Hintergrund weiterlaufen, auch nach einem Neustart.
Beim ersten Mal fragt er, ob Funnel für dein Tailnet erlaubt werden soll.

#### Im Assistenten

Füge einen eigenen MCP-Connector hinzu (oder in n8n den MCP Client Tool) mit:

| Feld | Wert |
|---|---|
| URL | `https://<deine-öffentliche-adresse>/mcp` |
| Transport | HTTP Streamable |
| Authentifizierung | Header `Authorization: Bearer <token>` (Einstellungen → MCP → **MCP-Token kopieren**) |

Clients, die nur eine Anmeldung per OAuth anbieten und kein Bearer-Token
schicken können, lassen sich so nicht verbinden.

**Claude im Web und auf dem Handy.** Öffne auf claude.ai (im Browser oder in
Claude Desktop) Anpassungen → Konnektoren, wähle **+** und füge einen
benutzerdefinierten Konnektor hinzu:

| Feld | Wert |
|---|---|
| Name | `RootMind` |
| URL | `https://<deine-öffentliche-adresse>/mcp` |
| Authentifizierung | **Keine Anmeldung** |
| Anfrage-Header | `Authorization` = `Bearer <token>` |

Der Konnektor ist dann auch in der Claude-App auf deinem Handy: Schalte ihn in
einem Chat ein und frag nach deinen Dokumenten. Claude verbindet sich von den
Servern von Anthropic; dein Computer muss also eingeschaltet und wach sein, und
was Claude über RootMind liest, geht an Anthropic. In der App zeigt **RootMind
in Claude im Web und auf dem Handy nutzen** (unter Remote) diese Werte mit
Kopier-Knöpfen.

Beachte:

- Wer das Token hat, kann über den Tunnel jedes indizierte Dokument lesen.
  Halte es geheim; wenn du den Tunnel beendest (oder den Fernzugriff
  ausschaltest), ist der öffentliche Zugang zu.
- Was ein Cloud-Assistent über RootMind liest (Suchtreffer und Dokumenttext),
  geht an den Anbieter dieses Assistenten.
- RootMind lehnt Anfragen ab, die eine Webseite aus einem Browser schickt
  (Anfragen mit nicht-lokalem `Origin`-Header); Anfragen von Server zu Server,
  wie sie der Assistent stellt, sind davon nicht betroffen.
