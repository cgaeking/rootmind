# Schnellstart

[English](QUICKSTART.md) | **Deutsch**

Vom Download zum ersten Suchergebnis in wenigen Minuten, unter Windows 10
oder 11 (x64). Die vollständige Dokumentation steht in der
[README](../README.de.md).

1. **Herunterladen:**
   [RootMind-Setup-x64.exe](https://github.com/cgaeking/rootmind/releases/latest/download/RootMind-Setup-x64.exe)
   aus dem [neuesten Release](https://github.com/cgaeking/rootmind/releases/latest).
2. **Installieren.** Starte den Installer. Er installiert RootMind für dein
   Konto, ohne Administratorrechte und ohne Python. Der Installer ist noch
   nicht codesigniert, deshalb zeigt Windows SmartScreen eventuell „Der
   Computer wurde durch Windows geschützt“: Wähle **Weitere Informationen**
   und dann **Trotzdem ausführen**.
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
sind die der deutschen Oberfläche.

## RootMind aus anderen Tools nutzen (MCP)

MCP-Clients bekommen dieselben lesenden Such-Tools und nutzen ihr eigenes
Modell; RootMind braucht für sie keinen API-Key.

- **Claude Desktop und Claude Code** starten das mitgelieferte Backend als
  MCP-Server über stdio (`rootmind-backend.exe mcp`). Die Konfiguration für
  beide steht unter [MCP-Clients verbinden](../README.de.md#mcp-clients-verbinden).
- **n8n und andere entfernte Clients** verbinden sich über HTTP. Schalte unter
  Einstellungen → **Hintergrunddienst** (braucht ein Administratorkonto) den
  Dienst mit **MCP über HTTP anbieten** ein; die Einstellungen zeigen dann den
  Endpunkt und ein Bearer-Token zum Kopieren. Siehe
  [MCP von n8n aus erreichen](../README.de.md#mcp-von-n8n-aus-erreichen).
