# MCP Registry server.json schema

The JSON schema of `server.json` for the official [MCP Registry](https://registry.modelcontextprotocol.io),
used to validate the rendered `server.json` without a network download (`scripts/registry_server_json.py`,
its tests and the `mcp-registry` job of `release.yml`).

| File | Source |
| --- | --- |
| `server.schema.2025-12-11.json` | `https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json` |

Downloaded unchanged on 2026-10-25 (SHA-256
`3fba09590c99f61735d234822279f4223fab9e300c0a81e81c91ab62a4114de0`). 2025-12-11 is the current
schema version of the registry (`CurrentSchemaVersion` in `pkg/model/constants.go` of
[modelcontextprotocol/registry](https://github.com/modelcontextprotocol/registry) at `v1.8.4`); the
registry rejects a `server.json` with an older `$schema`. The schema is part of the MCP project and
licensed under the MIT License and the Apache License 2.0; see `LICENSE` in this folder (copied from
the same repository at `v1.8.4`).

To move to a newer schema version, download the file its `$id` names into this folder as
`server.schema.<date>.json`, change `$schema` in `../server.json`, and update this README.
`scripts/check_versions.py` fails when `$schema` names a version that has no file here.
