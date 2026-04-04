# pyATS MCP Server

[![Trust Score](https://archestra.ai/mcp-catalog/api/badge/quality/automateyournetwork/pyATS_MCP)](https://archestra.ai/mcp-catalog/automateyournetwork__pyats_mcp)

This project implements a Model Context Protocol (MCP) Server that wraps Cisco pyATS and Genie functionality. It enables structured, model-driven interaction with **Modern IOS-XE** network devices (including Catalyst 9K and IOL) using the JSON-RPC 2.0 protocol over STDIO.

🚨 **Note**: This server is optimized for **Modern IOS-XE** (and equivalents like IOL) to ensure seamless EVPN VXLAN and MVPN provisioning.

---

## 🔧 Core Capabilities

- **Modern IOS-XE Optimization**: Full support for the latest configuration standards (e.g., Auto RD/RT, L3VNI/L2VNI).
- **Intelligent EVPN Provisioning**: Lifecycle-based tools for Day-1 Fabric, Day-2 L2VNI/L3VNI, and NG-MVPN.
- **Device Role-Awareness**: Built-in logic for roles: **Leaf (LF)**, **Spine (SP)**, **Border Leaf (BL)**, and **Border Gateway (BGW)** with automatic **RT Stitching**.
- **Token Economy (Genie Dq)**: Built-in **Genie Dictionary Query (Dq)** support to filter large JSON outputs and save LLM context window tokens.
- **Ad-hoc Connectivity**: Execute commands on any device without a `testbed.yaml` file.
- **Dynamic Device Management**: Add devices to the pyATS testbed on-the-fly via natural language chat.
- **Secure Configuration**: Mandatory blocking of unsafe commands (erase, reload, etc.) and validation using Pydantic.

---

## 🚀 Getting Started

### 1. Set Your Testbed Path
The server uses `testbed.yaml` by default. You can define your environment:
```bash
export PYATS_TESTBED_PATH=/path/to/your/testbed.yaml
```

### 2. Run the Server
```bash
python3 pyats_mcp_server.py
```

### 3. Docker Mode
```bash
docker build -t pyats-mcp-server .
docker run -i --rm -v /your/testbed/path:/app -e PYATS_TESTBED_PATH=/app/testbed.yaml pyats-mcp-server
```

---

## ⚡ Performance & Token Optimization (The Golden Workflow)

To minimize latency and token costs, especially when using **Local LLMs**, follow the **Discovery-First Workflow**:

1.  **Step 1 (Explore)**: Use `pyats_discover_json_structure` to get a tiny 'key-only' map of the data structure (Avoids massive payloads).
2.  **Step 2 (Map)**: Identify the exact JSON path you need (e.g., `info.vrf.default.neighbors`).
3.  **Step 3 (Filter)**: Call `pyats_learn_feature` or `pyats_run_show_command` with the `filter_query` (Genie Dq).

**Why?** Transferring a 50,000 token JSON is the primary bottleneck. Filtering it down to 50 tokens on the server side is **10x faster** than moving to a more powerful LLM or upgrading your network.

---

## 🧠 Specialized Tools

### Connectivity & Device Management
- **`pyats_run_command_adhoc`**: (One-Shot) Execute a command on any device instantly using IP, username, and password.
- **`mcp_pyats_add_device_to_testbed`**: Registers a new device to the current session's testbed for subsequent tool calls.

### Feature Learning & Troubleshooting
- **`pyats_learn_feature`**: Learns a network feature (OSPF, BGP, etc.) as a deep JSON model. 
  - *Token Tip*: Use `filter_query` (Dq) to get only what you need.
- **`pyats_run_show_command`**: Runs show commands with optional Genie parsing and Dq filtering.
- **`pyats_verify_bgp_convergence`**: Smart polling for BGP peer state before checking routing tables.

### EVPN VXLAN Fabric (Day-1 & Day-2)
- **`pyats_provision_evpn_fabric`**: Initializes BGP L2VPN EVPN, NVE source, and required BGP router-id/ASN for **Auto RD/RT**.
- **`pyats_add_l3vni`**: Adds Tenant VRF, L3VNI, and VNI-to-VLAN mapping. 
  - *Smart Logic*: Automatically applies `stitching` RTs if the device role is **Border Leaf (BL)** or **Border Gateway (BGW)**.
- **`pyats_add_l2vni`**: Provisions L2 extensions across the fabric with optional Anycast Gateway.
- **`pyats_provision_mvpn`**: Adds NG-MVPN (BGP signaling) for tenant VRFs.

---

## 🪙 Token Saving with Genie Dq

Network outputs can be massive. This server supports **Genie Dq** (Dictionary Query) directly in the tools.

**Example: Ad-hoc Command (No Testbed Required)**
```json
{
  "method": "tools/call",
  "params": {
    "name": "pyats_run_command_adhoc",
    "arguments": {
      "ip": "172.20.20.5",
      "os_type": "iosxe",
      "username": "admin",
      "password": "admin",
      "command": "show version"
    }
  }
}
```

**Example: Token-Saving Dq Filter**
```json
{
  "method": "tools/call",
  "params": {
    "name": "pyats_run_show_command",
    "arguments": {
      "device_name": "Leaf-01",
      "command": "show ip ospf neighbor",
      "filter_query": "info.vrf.default.address_family.ipv4.instance.1.areas.0.0.0.0.interfaces.GigabitEthernet1.neighbors"
    }
  }
}
```
*Instead of 1000 lines, you get exactly the 5 lines you need.*

---

## 🔒 Security & Validation

- **Safe Configuration**: `pyats_apply_configuration` and `pyats_direct_ssh_configure` block dangerous commands like `write erase`, `reload`, or `delete`.
- **BGP Strict Rules**: Automatically enforces `no bgp default ipv4-unicast` and explicit neighbor activation for EVPN stability.
- **Input Guard**: All tool inputs are validated via Pydantic to prevent CLI injection or malformed parameters.

---

## 📜 Example Integration (Claude/LangGraph)

```json
{
  "mcpServers": {
    "pyats": {
      "command": "python3",
      "args": ["/path/to/pyats_mcp_server.py"],
      "env": {
        "PYATS_TESTBED_PATH": "/path/to/testbed.yaml"
      }
    }
  }
}
```

# Reference

John Capobianco
https://github.com/automateyournetwork/pyATS_MCP
