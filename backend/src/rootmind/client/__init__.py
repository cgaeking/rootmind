"""Command-line client of a running RootMind service (``rootmind status``, ``folders``, ...).

The commands talk to the service's REST API with the REST token, the way the
desktop app and the Claude Desktop bundle do:

- :mod:`.discovery` finds the service of this account (``service status``),
  checks that the service's own process listens on its port and reads the
  token from its protected file;
- :mod:`.rest` sends the requests (no proxy, no redirects; the token goes
  only to the address that was checked);
- :mod:`.commands` holds the commands and their output (``--json`` for
  scripts).

Tokens and API keys are never printed and never taken from the command line.
"""
