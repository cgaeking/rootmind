// What the "connect a client" dialogs show and copy (Settings › RootMind
// service): Claude Desktop, Claude Code, n8n and a Claude custom connector.

/** rootmind.mcpb of the latest release (Claude Desktop installs it on a double-click). */
export const MCPB_URL =
  "https://github.com/cgaeking/rootmind/releases/latest/download/rootmind.mcpb";
/** The ngrok dashboard: the authtoken and the free static domain. */
export const NGROK_DASHBOARD_URL = "https://dashboard.ngrok.com/";
/** claude.ai, where custom connectors are added (Customize › Connectors). */
export const CLAUDE_URL = "https://claude.ai/";

/** The token placeholder shown in place of the real token. */
export const TOKEN_PLACEHOLDER = "<token>";

/** The Claude Code command that adds RootMind as an HTTP MCP server. */
export function claudeCodeCommand(endpoint: string, token: string): string {
  return `claude mcp add --transport http rootmind ${endpoint} --header "Authorization: Bearer ${token}"`;
}

/** The fields of a Claude custom connector for the public endpoint. */
export function claudeConnector(endpoint: string, token: string) {
  return {
    name: "RootMind",
    url: endpoint,
    header: "Authorization",
    value: `Bearer ${token}`,
  };
}
