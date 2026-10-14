# Schnellstart

[English](QUICKSTART.md) | **Deutsch**

Vom Download zum ersten Suchergebnis in wenigen Minuten, unter Windows 10
oder 11 (x64) mit einem Administratorkonto. Die vollständige Dokumentation
steht in der [README](../README.de.md). Unter Linux installierst du das .deb,
das .rpm oder das AppImage wie in
[Installation unter Linux](../README.de.md#installation-unter-linux)
beschrieben; RootMind richtet seinen Dienst beim ersten Start ein, und die
Schritte 3 bis 5 sind dieselben.

1. **Herunterladen:**
   [RootMind-Setup-x64.exe](https://github.com/cgaeking/rootmind/releases/latest/download/RootMind-Setup-x64.exe)
   aus dem [neuesten Release](https://github.com/cgaeking/rootmind/releases/latest).
2. **Installieren.** Starte den Installer und bestätige die
   Administrator-Abfrage einmal; Python ist nicht nötig. Er installiert die
   App und richtet den RootMind-Dienst ein, der deinen Index aktuell hält und
   mit Windows startet. Der Installer ist noch nicht codesigniert, deshalb
   zeigt Windows SmartScreen eventuell „Der Computer wurde durch Windows
   geschützt“: Wähle **Weitere Informationen** und dann **Trotzdem
   ausführen**.
3. **Ordner hinzufügen.** Starte RootMind, öffne **Ordner** und füge einen
   Ordner mit deinen Dokumenten hinzu; die Änderung wird sofort gespeichert.
   Die Indizierung beginnt innerhalb etwa einer Minute; **Status** zeigt den
   Fortschritt.
4. **Suchen.** Öffne **Chat**, setze den Haken bei **Nur Suche**, gib ein Wort
   oder eine Wortgruppe aus deinen Dokumenten ein und drücke Enter. Die Suche
   braucht keinen API-Key.
5. **Fragen stellen** (optional). Füge unter **Einstellungen** einen API-Key
   von Anthropic, OpenAI oder xAI ein und klicke daneben auf **Speichern**.
   RootMind erkennt den Anbieter meist am Key (sonst wähle ihn vorher unter
   **Anbieter**), prüft den Key und lädt die Modell-Liste; ein Modell wird
   automatisch ausgewählt. Entferne dann den Haken bei **Nur Suche** und
   stelle deine Frage im Chat; die Antworten nennen ihre Quellen. Die Frage
   und Auszüge aus den Dokumenten gehen an diesen Anbieter.

Die App folgt der Anzeigesprache von Windows (Deutsch oder Englisch);
Einstellungen → **Darstellung** → **Sprache** stellt sie um. Die Namen oben
sind die der deutschen Oberfläche. Du kommst von RootMind 1.x? Der Installer
ersetzt es, aber Ordner und API-Key fügst du erneut hinzu (siehe
[Umstieg von 1.x](../README.de.md#umstieg-von-1x)).

## RootMind aus anderen Tools nutzen (MCP)

MCP-Clients bekommen dieselben lesenden Such-Tools und nutzen ihr eigenes
Modell; RootMind braucht für sie keinen API-Key. Der RootMind-Dienst stellt
standardmäßig MCP über HTTP auf 127.0.0.1 bereit: Einstellungen →
**RootMind-Dienst** zeigt den **MCP-Endpunkt** und kopiert ihn und das
Bearer-Token (**Token kopieren**).

- **Claude Desktop:** lade
  [rootmind.mcpb](https://github.com/cgaeking/rootmind/releases/latest/download/rootmind.mcpb)
  herunter und doppelklicke die Datei; Claude Desktop installiert sie, ohne
  dass etwas einzurichten ist (siehe
  [RootMind in Claude Desktop nutzen](../README.de.md#rootmind-in-claude-desktop-nutzen)).
- **Claude Code:**
  `claude mcp add --transport http rootmind <endpoint> --header "Authorization: Bearer <token>"`.
- **n8n und andere entfernte Clients:** **Kopieren** unter **n8n (MCP Client
  Tool)** kopiert die Werte für den Knoten MCP Client Tool von n8n. Siehe
  [MCP von n8n aus erreichen](../README.de.md#mcp-von-n8n-aus-erreichen).
