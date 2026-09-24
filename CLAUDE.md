# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Goal

A Python MCP (Model Context Protocol) server that exposes an Ansible runner to the Rancher AI Assistant (Liz). One pod runs both Ansible and the MCP server; Liz invokes ad-hoc modules, playbooks, and inventory queries over HTTP. Part of the `advanced-mcp-demo` stack (see `../liz-demos/advanced-mcp-demo/README.md`).

This is a demo project. Credentials are in plain k8s Secrets; SSH is password-based; the whole stack lives behind a VPN and gets torn down after the demo.

## Architecture

Single container, two responsibilities:

- **Ansible runtime** — base image `willhallonline/ansible:2.16-debian-bookworm` provides `ansible`, `ansible-playbook`, `ansible-inventory`, `ansible-doc`, `sshpass`, `openssh-client`.
- **MCP server** — `server.py` (FastMCP) shells out to those binaries and returns summarized, truncated JSON to Liz.

The MCP pod (`ns: cattle-ai-agent-system`) is independent from the operator-facing `ansible-runner` pod (`ns: liz-toolshed`). They share configmap content but live in different namespaces — duplicated YAML, not a shared volume.

```
Liz ──HTTP──> ansible-mcp pod ──ssh──> downstream worker / server nodes
                  │
                  ├── /etc/ansible/{ansible.cfg, hosts, group_vars/all.yml}   (from ConfigMap)
                  └── /playbooks/*.yml                                          (from ConfigMap)
```

Why no separate "ansible app" pod for the MCP to call: Ansible has no HTTP API. The MCP server needs the ansible binary in-process. `ansible-runner` in `liz-toolshed` stays around as the operator's `kubectl exec` debug target; this pod is purely Liz's.

## MCP Tools

| Tool | What it does |
|------|--------------|
| `list_inventory()` | Groups (with hosts + children) and per-host vars from `ansible-inventory --list` |
| `list_hosts(group="all")` | Recursive hostnames in a group via `ansible <group> --list-hosts` |
| `list_playbooks()` | Files in `/playbooks` with first-play name, hosts, plays_count |
| `ping_hosts(hosts="all")` | Compact `{reachable: [...], unreachable: [...]}` |
| `run_adhoc(module, hosts, args, become, check, timeout_sec)` | Single Ansible module against hosts (the workhorse — covers every builtin and collection module) |
| `run_playbook(name, limit, extra_vars, tags, check, timeout_sec)` | Playbook run with structured recap + failure detail |
| `describe_module(name)` | Trimmed `ansible-doc -j <name>` output (description, options, examples) |

### Output shape conventions

`run_adhoc`:
```jsonc
{
  "rc": 0, "duration_sec": 1.3,
  "results": {
    "host1": {
      "status": "success",        // or "changed" | "failed" | "skipped" | "unreachable"
      "changed": false,
      "data": { /* module-specific output, truncated if >4000 chars */ }
    }
  }
}
```

`run_playbook`:
```jsonc
{
  "rc": 0, "duration_sec": 14.7,
  "recap": { "host1": {"ok": 5, "changed": 2, "failed": 0, "unreachable": 0, "skipped": 0} },
  "tasks": [{"play": "...", "task": "...", "results": {"host1": "changed"}}],
  "failures": []   // full detail (msg, stdout/stderr truncated to 500ch each) for failed/unreachable
}
```

Server-side truncation: stdout/stderr 500–1000ch, `ansible_facts` dicts 4000ch — if a dict is too large, returns `{_truncated: true, _total_keys, _keys_sample, _hint}` and tells the caller to narrow the query.

## Run

### Local
```bash
pip install -r requirements.txt
# Requires ansible binary in PATH and /etc/ansible/{ansible.cfg,hosts} configured
PLAYBOOK_DIR=./local_playbooks python3 server.py
```

### Container build & push
```bash
docker build -t docker.io/olegvorobyov90/ansible-mcp:0.1 .
docker push docker.io/olegvorobyov90/ansible-mcp:0.1
```

### Deploy
```bash
# Apply manifests (uses cattle-ai-agent-system namespace; create it if missing)
kubectl create namespace cattle-ai-agent-system --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f k8s-mcp/

# Verify
kubectl -n cattle-ai-agent-system get pods -l app=ansible-mcp
kubectl -n cattle-ai-agent-system logs -l app=ansible-mcp

# Replicate to demo project (final home for the demo build)
cp k8s-mcp/* ../liz-demos/advanced-mcp-demo/k8s/ansible-mcp/
```

**Liz MCP URL** (registered by the `AIAgentConfig` in `k8s-mcp/ai-agent-config.yaml`):
```
http://ansible-mcp.cattle-ai-agent-system.svc.cluster.local:8000/mcp
```

## Smoke-testing MCP tools

```bash
kubectl -n cattle-ai-agent-system port-forward svc/ansible-mcp 8000:8000

# List tools (expect 7)
curl -s -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/list","params":{},"id":1}' | jq '.result.tools[].name'

# Inventory
curl -s -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"list_inventory","arguments":{}},"id":2}' | jq .

# Ping the workers
curl -s -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"ping_hosts","arguments":{"hosts":"downstream_workers"}},"id":3}' | jq .

# Uptime via run_adhoc — proves natural-language pattern works end-to-end
curl -s -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"run_adhoc","arguments":{"module":"command","hosts":"downstream-worker-1","args":{"cmd":"uptime -p"}}},"id":4}' | jq .
```

## Calling from n8n

n8n's HTTP Request node can hit `/mcp` directly with the same JSON-RPC envelope. For the demo's patch flow, the call is:

```json
{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "params": {
    "name": "run_playbook",
    "arguments": {
      "name": "patch_and_reboot.yml",
      "limit": "downstream-worker-1",
      "extra_vars": {"packages": ["bash", "openssl"]}
    }
  },
  "id": 1
}
```

n8n sets URL = `http://ansible-mcp.cattle-ai-agent-system.svc.cluster.local:8000/mcp`, method POST, header `Content-Type: application/json`. The `extra_vars.packages` list comes from earlier nodes in the n8n workflow (translated from the NV CVE → package list).

## Design notes

- **Subprocess + `ANSIBLE_STDOUT_CALLBACK=json`** for all ansible invocations. Ad-hoc runs additionally need `ANSIBLE_LOAD_CALLBACK_PLUGINS=1` to honor the env var. The `yaml` callback in `ansible.cfg` stays as the default for operator-friendly `kubectl exec` runs.
- **Synchronous tool calls.** No job-id polling. `run_playbook` has a 600s timeout to comfortably cover patch + reboot + reconnect; `run_adhoc` defaults to 120s.
- **No `humanValidationTools`.** Approval for destructive operations lives upstream (Mattermost button → n8n → MCP). The MCP should not double-prompt.
- **`become=True` by default.** The `group_vars/all.yml` already sets `ansible_become: true`. When a tool call sets `become=False`, the server passes `-e 'ansible_become=false'` to override.
- **`run_playbook` path safety.** `name` must be a basename and end in `.yml`/`.yaml`. No path traversal.
- **Module universe is open by design.** `run_adhoc` accepts any module name Ansible can resolve, so the LLM has access to every builtin + collection module without per-module schemas. `describe_module` is the escape hatch for parameter lookup.
