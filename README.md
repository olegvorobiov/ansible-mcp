# Liz AI Agent Demo: Ansible MCP 🦎✨

⚠️ **Disclaimer:** Just like the rest of this demo stack, this repository is a crazy, "vibe coded" experiment! It should be taken with a massive grain of salt. Credentials are in plain text, SSH is password-based, and it's designed to live safely behind a VPN before being torn down. Do not use this architecture in production!

## About This Project

This project provides a Python MCP (Model Context Protocol) server that exposes an Ansible runner to the **Rancher AI Assistant (Liz)**. It bridges the gap between Liz's AI capabilities and infrastructure automation, allowing Liz to invoke ad-hoc Ansible modules, run playbooks, and query inventory directly over HTTP.

It forms a critical part of the `advanced-mcp-demo` stack, proving that an LLM can safely and effectively patch, reboot, and manage downstream worker nodes using natural language converted into Ansible commands.

## What's Inside

The repository is split between the core Python server logic and the Kubernetes manifests required to deploy it:

* **`server.py` & `requirements.txt`:** The core FastMCP server that acts as the HTTP interface for Liz. It receives JSON-RPC requests, shells out to the local Ansible binaries, and returns summarized, truncated JSON back to the AI.
* **`Dockerfile`:** Packages the Python server alongside a base Debian image pre-loaded with Ansible binaries (`willhallonline/ansible:2.16-debian-bookworm`).
* **`k8s/`:** Base Kubernetes manifests (Deployments, ConfigMaps for playbooks/inventory, and Secrets).
* **`k8s-mcp/`:** The specific manifests for deploying this into the `cattle-ai-agent-system` namespace, including the vital `ai-agent-config.yaml` that registers the MCP server with Liz.
* **`CLAUDE.md`:** **Read this file!** It contains the deep-dive technical documentation, architecture design notes, and exact `curl` commands for smoke testing the MCP tools.

## How it Works

Because Ansible doesn't have a native HTTP API, this setup runs a single container with a split personality:
1. **The Ansible Runtime:** Knows how to SSH into downstream nodes and run playbooks.
2. **The MCP Server:** Listens for Liz's HTTP requests and translates them into local terminal commands (`ansible`, `ansible-playbook`, etc.).

Liz is equipped with 7 distinct tools through this MCP:
* `list_inventory()` & `list_hosts()`
* `list_playbooks()` & `run_playbook()`
* `ping_hosts()`
* `run_adhoc()` (The main workhorse for running any Ansible module)
* `describe_module()`

## Getting Started

If you are picking up this project, your best friend is the `CLAUDE.md` file located in this directory. It contains exact copy-paste commands for:
* Running the server locally for testing.
* Building and pushing the Docker container.
* Deploying the manifests to Kubernetes.
* Sending JSON-RPC payloads via `curl` or tools like **n8n** to trigger playbooks.

Have fun, explore the code, and enjoy experimenting with Liz's infrastructure superpowers!