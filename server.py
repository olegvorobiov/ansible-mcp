import json
import os
import subprocess
import time

import yaml
from fastmcp import FastMCP

PLAYBOOK_DIR = os.environ.get("PLAYBOOK_DIR", "/playbooks")

mcp = FastMCP("Ansible Operations")


# ---------- helpers ----------

def _truncate(text: str, max_chars: int) -> str:
    if not text:
        return text
    text = str(text)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"... [truncated {len(text) - max_chars} chars]"


def _truncate_dict(d, max_chars: int):
    """If a dict's JSON form exceeds max_chars, return a summary with key sample instead."""
    try:
        s = json.dumps(d)
    except (TypeError, ValueError):
        return {"_unserializable": True}
    if len(s) <= max_chars:
        return d
    if isinstance(d, dict):
        keys = list(d.keys())
        return {
            "_truncated": True,
            "_total_keys": len(keys),
            "_keys_sample": keys[:50],
            "_size_chars": len(s),
            "_hint": "Output too large. Use a more specific module/filter (e.g. setup with args={'filter': 'ansible_distribution*'}) to narrow the response.",
        }
    return {"_truncated": True, "_size_chars": len(s)}


def _host_status(info: dict) -> str:
    if info.get("unreachable"):
        return "unreachable"
    if info.get("failed"):
        return "failed"
    if info.get("skipped"):
        return "skipped"
    if info.get("changed"):
        return "changed"
    return "success"


def _run(cmd, env=None, timeout=120):
    """Run a subprocess, return (rc, stdout, stderr, duration_sec). Raises on timeout."""
    full_env = {**os.environ, **(env or {})}
    t0 = time.monotonic()
    result = subprocess.run(
        cmd,
        env=full_env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.returncode, result.stdout, result.stderr, round(time.monotonic() - t0, 1)


def _ansible_env(extra: dict | None = None) -> dict:
    env = {
        "ANSIBLE_STDOUT_CALLBACK": "json",
        # ad-hoc `ansible` ignores stdout callbacks unless this is set
        "ANSIBLE_LOAD_CALLBACK_PLUGINS": "1",
    }
    if extra:
        env.update(extra)
    return env


def _summarize_playbook(data: dict, rc: int, duration: float, stderr: str) -> dict:
    tasks_out = []
    failures = []
    for play in data.get("plays", []):
        play_name = (play.get("play") or {}).get("name", "")
        for task in play.get("tasks", []):
            task_name = (task.get("task") or {}).get("name", "")
            per_host = {}
            for host, info in (task.get("hosts") or {}).items():
                status = _host_status(info)
                per_host[host] = status
                if status in ("failed", "unreachable"):
                    failures.append({
                        "host": host,
                        "play": play_name,
                        "task": task_name,
                        "msg": info.get("msg", ""),
                        "stdout": _truncate(info.get("stdout", ""), 500),
                        "stderr": _truncate(info.get("stderr", ""), 500),
                    })
            tasks_out.append({"play": play_name, "task": task_name, "results": per_host})

    out = {
        "rc": rc,
        "duration_sec": duration,
        "recap": data.get("stats", {}),
        "tasks": tasks_out,
        "failures": failures,
    }
    if rc != 0 and not failures and stderr:
        out["stderr"] = _truncate(stderr, 1000)
    return out


_ADHOC_SYSTEM_KEYS = {
    "changed", "failed", "skipped", "unreachable", "msg",
    "stdout", "stderr", "stdout_lines", "stderr_lines",
    "invocation", "action", "rc", "start", "end", "delta", "cmd",
    "warnings", "deprecations",
}


def _summarize_adhoc(data: dict, rc: int, duration: float, stderr: str) -> dict:
    results = {}
    plays = data.get("plays", []) or []
    if plays:
        tasks = plays[0].get("tasks") or [{}]
        task = tasks[0]
        for host, info in (task.get("hosts") or {}).items():
            status = _host_status(info)
            entry = {
                "status": status,
                "changed": bool(info.get("changed", False)),
            }
            for k in ("msg", "stdout", "stderr"):
                v = info.get(k)
                if v:
                    entry[k] = _truncate(str(v), 1000)
            # Modules like setup/package_facts/service_facts return ansible_facts
            if "ansible_facts" in info:
                entry["data"] = _truncate_dict(info["ansible_facts"], max_chars=4000)
            else:
                # Promote any other module-specific keys
                non_system = {
                    k: v for k, v in info.items()
                    if not k.startswith("_ansible") and k not in _ADHOC_SYSTEM_KEYS
                }
                if non_system:
                    entry["data"] = _truncate_dict(non_system, max_chars=4000)
            results[host] = entry

    out = {"rc": rc, "duration_sec": duration, "results": results}
    if rc != 0 and not results and stderr:
        out["stderr"] = _truncate(stderr, 1000)
    return out


def _execute(cmd, env, timeout, kind):
    try:
        rc, stdout, stderr, duration = _run(cmd, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"rc": -1, "error": f"timeout after {timeout}s", "duration_sec": timeout}
    try:
        data = json.loads(stdout) if stdout.strip() else {}
    except json.JSONDecodeError:
        return {
            "rc": rc,
            "duration_sec": duration,
            "error": "Failed to parse Ansible JSON output (callback may have been suppressed).",
            "stdout": _truncate(stdout, 2000),
            "stderr": _truncate(stderr, 2000),
        }
    if kind == "playbook":
        return _summarize_playbook(data, rc, duration, stderr)
    return _summarize_adhoc(data, rc, duration, stderr)


# ---------- tools ----------

@mcp.tool()
def list_inventory() -> dict:
    """List the full Ansible inventory: groups (with their hosts and children) and per-host variables.

    Use this to see what hosts and groups exist before targeting them with run_adhoc or run_playbook.
    """
    try:
        rc, stdout, stderr, _ = _run(["ansible-inventory", "--list"], timeout=15)
    except subprocess.TimeoutExpired:
        return {"error": "ansible-inventory timed out"}
    if rc != 0:
        return {"error": stderr.strip() or "ansible-inventory failed"}
    try:
        inv = json.loads(stdout)
    except json.JSONDecodeError as e:
        return {"error": f"Failed to parse inventory: {e}"}

    hostvars = (inv.get("_meta") or {}).get("hostvars", {}) or {}
    groups = {}
    for key, val in inv.items():
        if key == "_meta":
            continue
        if not isinstance(val, dict):
            continue
        hosts = val.get("hosts") or []
        children = val.get("children") or []
        if not hosts and not children:
            # skip empty implicit groups like 'ungrouped' when empty
            continue
        entry = {}
        if hosts:
            entry["hosts"] = hosts
        if children:
            entry["children"] = children
        groups[key] = entry

    return {"groups": groups, "hosts": hostvars}


@mcp.tool()
def list_hosts(group: str = "all") -> list[str]:
    """List the hostnames in an inventory group (recursive). Default 'all' returns every managed host.

    Use this to disambiguate when the user gives a partial name (e.g. 'worker' → 'downstream-worker-1').
    """
    try:
        rc, stdout, stderr, _ = _run(["ansible", group, "--list-hosts"], timeout=10)
    except subprocess.TimeoutExpired:
        return []
    if rc != 0:
        return []
    lines = stdout.strip().split("\n")
    # First line is something like "  hosts (N):", rest are hostnames
    hosts = []
    for line in lines[1:]:
        h = line.strip()
        if h:
            hosts.append(h)
    return hosts


@mcp.tool()
def list_playbooks() -> list[dict]:
    """List the playbooks available in /playbooks with their first play's name and target hosts.

    Use this before run_playbook to discover what is available to run.
    """
    out = []
    if not os.path.isdir(PLAYBOOK_DIR):
        return out
    for name in sorted(os.listdir(PLAYBOOK_DIR)):
        if not (name.endswith(".yml") or name.endswith(".yaml")):
            continue
        path = os.path.join(PLAYBOOK_DIR, name)
        entry = {"name": name}
        try:
            with open(path) as f:
                plays = yaml.safe_load(f)
            if isinstance(plays, list) and plays:
                first = plays[0] or {}
                entry["play_name"] = first.get("name", "")
                entry["hosts"] = first.get("hosts", "")
                entry["plays_count"] = len(plays)
        except Exception as e:
            entry["parse_error"] = str(e)
        out.append(entry)
    return out


@mcp.tool()
def ping_hosts(hosts: str = "all") -> dict:
    """Run ansible.builtin.ping against the matched hosts to verify SSH + Python work.

    Returns a compact {reachable: [...], unreachable: [...]} list. Use after reboots
    or to triage 'is this host alive?' questions.
    """
    cmd = ["ansible", hosts, "-m", "ping"]
    env = _ansible_env()
    result = _execute(cmd, env, timeout=30, kind="adhoc")
    reachable, unreachable = [], []
    for host, info in (result.get("results") or {}).items():
        if info.get("status") == "success":
            reachable.append(host)
        else:
            unreachable.append(host)
    return {"reachable": reachable, "unreachable": unreachable}


@mcp.tool()
def run_adhoc(
    module: str,
    hosts: str = "all",
    args: dict | None = None,
    become: bool = True,
    check: bool = False,
    timeout_sec: int = 120,
) -> dict:
    """Run a single Ansible module against one or more hosts ad-hoc.

    This is the primary tool for OS-level inspection and one-shot operations on managed
    servers. It covers every Ansible builtin and collection module. Use describe_module(name)
    when you are unsure of a module's parameters.

    Common patterns:
      - Uptime:              module="command", args={"cmd": "uptime -p"}
      - Disk usage:          module="command", args={"cmd": "df -h"}
      - Installed packages:  module="package_facts"  (returns ansible_facts.packages)
      - Running services:    module="service_facts"  (returns ansible_facts.services)
      - OS / kernel info:    module="setup", args={"filter": "ansible_distribution*,ansible_kernel"}
      - Kernel params:       module="command", args={"cmd": "sysctl -a"}
      - File contents:       module="slurp", args={"src": "/etc/os-release"}  (base64 in 'content')
      - Install a package:   module="package", args={"name": "bash", "state": "latest"}
      - Manage a service:    module="systemd", args={"name": "sshd", "state": "restarted"}
      - Reboot (sync):       module="reboot"  (waits for reconnect)

    Args:
      module: Ansible module name, e.g. 'command', 'package_facts', 'setup', 'service_facts'.
      hosts: Inventory pattern — hostname, group, or wildcard. Default 'all'.
      args: Dict of module parameters. For the command module use {"cmd": "..."}.
      become: Run with sudo. Default True. Set False for things that don't need root.
      check: Dry-run mode. Module reports what it would change without making changes.
      timeout_sec: Hard timeout on the ansible process. Default 120s.
    """
    cmd = ["ansible", hosts, "-m", module]
    if args:
        cmd += ["-a", json.dumps(args)]
    if not become:
        cmd += ["-e", "ansible_become=false"]
    if check:
        cmd += ["--check"]
    return _execute(cmd, env=_ansible_env(), timeout=timeout_sec, kind="adhoc")


@mcp.tool()
def run_playbook(
    name: str,
    limit: str | None = None,
    extra_vars: dict | None = None,
    tags: list[str] | None = None,
    check: bool = False,
    timeout_sec: int = 600,
) -> dict:
    """Run a playbook from /playbooks. Returns a structured recap with per-task per-host status
    and full detail on any failures.

    Args:
      name: Playbook filename (e.g. 'patch_and_reboot.yml'). Must be a basename ending in .yml/.yaml.
      limit: Restrict to a host/group pattern, e.g. 'downstream-worker-1' or 'downstream_workers'.
      extra_vars: Dict of vars to pass to the playbook (e.g. {"packages": ["bash"]}).
      tags: List of task tags to run. If omitted, all tasks run.
      check: Dry-run mode. Reports what would change without making changes.
      timeout_sec: Hard timeout on the ansible-playbook process. Default 600s (covers patch+reboot+reconnect).

    Output shape:
      {
        "rc": 0,                                    # 0 = success
        "duration_sec": 14.7,
        "recap":   {"host": {"ok": 5, "changed": 2, "failed": 0, ...}},
        "tasks":   [{"play": "...", "task": "...", "results": {"host": "changed"}}],
        "failures": []                              # full detail on any failed task-host pair
      }
    """
    if name != os.path.basename(name) or not (name.endswith(".yml") or name.endswith(".yaml")):
        return {"error": f"Invalid playbook name: {name!r}. Must be a basename ending in .yml or .yaml."}
    path = os.path.join(PLAYBOOK_DIR, name)
    if not os.path.isfile(path):
        return {"error": f"Playbook not found: {path}. Call list_playbooks() to see what's available."}

    cmd = ["ansible-playbook", path]
    if limit:
        cmd += ["-l", limit]
    if extra_vars:
        cmd += ["--extra-vars", json.dumps(extra_vars)]
    if tags:
        cmd += ["-t", ",".join(tags)]
    if check:
        cmd += ["--check"]
    return _execute(cmd, env=_ansible_env(), timeout=timeout_sec, kind="playbook")


@mcp.tool()
def describe_module(name: str) -> dict:
    """Look up an Ansible module's documentation: short description, parameters, examples.

    Use this before run_adhoc when you don't already know a module's parameter shape.
    Accepts builtin names ('ping', 'command') or fully-qualified collection names ('ansible.builtin.user').
    """
    try:
        rc, stdout, stderr, _ = _run(["ansible-doc", "-j", name], timeout=15)
    except subprocess.TimeoutExpired:
        return {"error": "ansible-doc timed out"}
    if rc != 0:
        return {"error": stderr.strip() or f"Module not found: {name}"}
    try:
        doc = json.loads(stdout)
    except json.JSONDecodeError as e:
        return {"error": f"Failed to parse ansible-doc output: {e}"}
    if not doc:
        return {"error": f"Empty doc for module: {name}"}

    module_key = next(iter(doc))
    full = doc[module_key] or {}
    d = full.get("doc") or {}

    options = {}
    for opt_name, opt in (d.get("options") or {}).items():
        desc = opt.get("description")
        if isinstance(desc, list):
            desc = " ".join(str(x) for x in desc)
        options[opt_name] = {
            "description": _truncate(desc or "", 300),
            "type": opt.get("type"),
            "required": bool(opt.get("required", False)),
            "default": opt.get("default"),
            "choices": opt.get("choices"),
        }

    examples = full.get("examples", "")
    if isinstance(examples, str):
        examples = _truncate(examples, 2000)

    description = d.get("description")
    if isinstance(description, list):
        description = " ".join(str(x) for x in description)
    description = _truncate(description or "", 1000)

    return {
        "name": module_key,
        "short_description": d.get("short_description", ""),
        "description": description,
        "options": options,
        "examples": examples,
    }


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8000)
