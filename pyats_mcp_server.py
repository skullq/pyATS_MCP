# pyats_fastmcp_server.py

import os
import re
import string
import sys
import json
import logging
import textwrap
import yaml
from pyats.topology import loader, Testbed, Device
from genie.libs.parser.utils import get_parser
from dotenv import load_dotenv

# Enforce Docker Execution
if os.getenv("RUNNING_IN_DOCKER") != "true":
    print("\n[ERROR] This server must be run inside a Docker container.", file=sys.stderr)
    print("Please use the following command to run it via Docker:", file=sys.stderr)
    print("  docker run -i --rm -v $(pwd):/app pyats-mcp-server\n", file=sys.stderr)
    sys.exit(1)

from pydantic import BaseModel, Field, ValidationError
from typing import Dict, Any, Optional
import asyncio
from enum import Enum
from functools import partial
import mcp.types as types
from mcp.server.fastmcp import FastMCP

# --- Basic Logging Setup ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("PyatsFastMCPServer")

# --- Load Environment Variables ---
load_dotenv()
# Use testbed.yaml in the current directory as a default if the environment variable is missing.
TESTBED_PATH = os.getenv("PYATS_TESTBED_PATH", "testbed.yaml")

if not os.path.exists(TESTBED_PATH):
    logger.warning(f"⚠️ Testbed file not found at {TESTBED_PATH}. Creating an empty testbed. Users can add devices dynamically via chat.")
    # Automatically initialize empty testbed file.
    with open(TESTBED_PATH, 'w') as f:
        yaml.dump({"testbed": {"name": "dynamic_chat_testbed"}, "devices": {}}, f)
else:
    logger.info(f"✅ Using existing testbed file: {TESTBED_PATH}")

# --- Pydantic Models for Input Validation ---
class DeviceCommandInput(BaseModel):
    device_name: str = Field(..., description="The name of the device in the testbed.")
    command: str = Field(..., description="The command to execute (e.g., 'show ip interface brief', 'ping 8.8.8.8').")

class ConfigInput(BaseModel):
    device_name: str = Field(..., description="The name of the device in the testbed.")
    config_commands: str = Field(..., description="Single or multi-line configuration commands.")

class DeviceOnlyInput(BaseModel):
    device_name: str = Field(..., description="The name of the device in the testbed.")

class LinuxCommandInput(BaseModel):
    device_name: str = Field(..., description="The name of the Linux device in the testbed.")
    command: str = Field(..., description="Linux command to execute (e.g., 'ifconfig', 'ls -l /home')")

class BGPAddressFamily(str, Enum):
    IPV4_UNICAST = "ipv4 unicast"
    IPV6_UNICAST = "ipv6 unicast"
    VPNV4_UNICAST = "vpnv4 unicast all"
    VPNV6_UNICAST = "vpnv6 unicast all"
    L2VPN_EVPN = "l2vpn evpn"
    ALL = "all"

# --- Core pyATS Helper Functions ---

def clean_output(output: str) -> str:
    """Clean ANSI escape sequences and non-printable characters."""
    # Remove ANSI escape sequences
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    output = ansi_escape.sub('', output)
    
    # Remove non-printable control characters
    output = ''.join(char for char in output if char in string.printable)
    
    return output

def _get_device_params(device_name: str) -> Dict[str, Any]:
    """Extract IP, username, and password from testbed without connecting."""
    try:
        testbed = loader.load(TESTBED_PATH)
        device = testbed.devices.get(device_name)
        if not device:
            raise ValueError(f"Device '{device_name}' not found in testbed.")
        
        # Extract connection info
        conn = device.connections.get('cli', {})
        ip = conn.get('ip')
        
        # Get credentials
        creds = device.credentials.get('default', {})
        username = creds.get('username')
        password = creds.get('password')
        
        if not ip or not username or not password:
            raise ValueError(f"Missing connectivity info for {device_name} (IP/User/Pass)")
            
        return {
            "ip": ip,
            "username": username,
            "password": password,
            "device_obj": device
        }
    except Exception as e:
        logger.error(f"Error extracting params for {device_name}: {e}")
        raise

def _offline_parse_sync(device_obj, command: str, raw_output: str) -> Any:
    """Use Genie to parse raw output without a live connection."""
    try:
        parser_gen = get_parser(command, device_obj)
        if not parser_gen:
            return raw_output
        
        # get_parser might return a single class or a list/tuple of classes
        if isinstance(parser_gen, (list, tuple)):
            parser_class = parser_gen[0]
        else:
            parser_class = parser_gen

        return parser_class(device=device_obj).parse(output=raw_output)
    except Exception as e:
        logger.warning(f"Offline parsing failed for '{command}': {e}")
        return raw_output

# --- Core pyATS Functions ---

async def run_show_command_async(device_name: str, command: str) -> Dict[str, Any]:
    """Execute a show command on a device using SSHPass + Offline Parsing."""
    try:
        # 1. Validate command
        disallowed_modifiers = ['|', 'include', 'exclude', 'begin', 'redirect', '>', '<', 'config', 'copy', 'delete', 'erase', 'reload', 'write']
        command_lower = command.lower().strip()
        
        if not command_lower.startswith("show"):
            return {"status": "error", "error": f"Command '{command}' is not a 'show' command."}
        
        for part in command_lower.split():
            if part in disallowed_modifiers:
                return {"status": "error", "error": f"Command '{command}' contains disallowed term '{part}'."}

        # 2. Get device params
        params = _get_device_params(device_name)
        
        # 3. Execute via Raw SSH (sshpass)
        logger.info(f"[{device_name}] Executing show command via SSHPass: '{command}'")
        res = await direct_ssh_execute(params['ip'], params['username'], params['password'], command)
        
        if res['status'] != 'success':
            return {"status": "error", "error": f"SSH Failure: {res.get('error') or res.get('error_output')}"}

        # 4. Perform Offline Parsing
        raw_output = res['output']
        # Remove the command echoed back and the prompt at the end
        # direct_ssh_execute already cleans some stuff, but we might need more
        
        parsed_output = _offline_parse_sync(params['device_obj'], command, raw_output)
        
        status = "completed" if isinstance(parsed_output, dict) else "completed_raw"
        return {"status": status, "device": device_name, "output": parsed_output}

    except Exception as e:
        logger.error(f"Error in run_show_command_async: {e}", exc_info=True)
        return {"status": "error", "error": str(e)}

async def apply_device_configuration_async(device_name: str, config_commands: str) -> Dict[str, Any]:
    """Apply configuration to a device using SSHPass."""
    try:
        # 1. Safety check
        if "erase" in config_commands.lower() or "write erase" in config_commands.lower():
            logger.warning(f"Rejected potentially dangerous command on {device_name}: {config_commands}")
            return {"status": "error", "error": "Potentially dangerous command detected (erase). Operation aborted."}

        # 2. Get device params
        params = _get_device_params(device_name)
        
        # 3. Prepare command string (ensure config mode)
        full_config = f"conf t\n{config_commands}\nend\nwrite memory"
        
        # 4. Execute via Raw SSH
        logger.info(f"[{device_name}] Applying configuration via SSHPass...")
        res = await direct_ssh_execute(params['ip'], params['username'], params['password'], full_config)
        
        if res['status'] != 'success':
            return {"status": "error", "error": f"SSH Config Failure: {res.get('error') or res.get('error_output')}"}

        return {
            "status": "success", 
            "message": f"Configuration applied on {device_name} via SSHPass.", 
            "output": res['output']
        }

    except Exception as e:
        logger.error(f"Error in apply_device_configuration_async: {e}", exc_info=True)
        return {"status": "error", "error": f"Configuration error: {e}"}

async def execute_learn_config_async(device_name: str) -> Dict[str, Any]:
    """Learn device configuration using SSHPass."""
    try:
        params = _get_device_params(device_name)
        res = await direct_ssh_execute(params['ip'], params['username'], params['password'], "show run brief")
        
        if res['status'] != 'success':
            return {"status": "error", "error": res.get('error_output')}
            
        cleaned_output = clean_output(res['output'])
        return {
            "status": "completed_raw",
            "device": device_name,
            "output": {"raw_output": cleaned_output}
        }
    except Exception as e:
        logger.error(f"Error learning config: {e}", exc_info=True)
        return {"status": "error", "error": f"Error learning config: {e}"}

async def learn_feature_async(device_name: str, feature: str, filter_query: Optional[str] = None) -> Dict[str, Any]:
    """
    [DEPRECATED/SSHPass Mode] Genie Ops (learn) is not natively supported in raw SSH mode.
    Falling back to 'show <feature> summary' or similar.
    """
    cmd_map = {
        "ospf": "show ip ospf",
        "bgp": "show ip bgp summary",
        "interface": "show ip interface brief",
        "routing": "show ip route"
    }
    command = cmd_map.get(feature.lower(), f"show {feature}")
    logger.info(f"[{device_name}] learn_feature fallback to show: '{command}'")
    return await run_show_command_async(device_name, command)

async def execute_learn_logging_async(device_name: str) -> Dict[str, Any]:
    """Learn device logging using SSHPass."""
    try:
        params = _get_device_params(device_name)
        res = await direct_ssh_execute(params['ip'], params['username'], params['password'], "show logging")
        
        if res['status'] != 'success':
            return {"status": "error", "error": res.get('error_output')}
            
        cleaned_output = clean_output(res['output'])
        return {
            "status": "completed_raw",
            "device": device_name,
            "output": {"raw_output": cleaned_output}
        }
    except Exception as e:
        logger.error(f"Error learning logs: {e}", exc_info=True)
        return {"status": "error", "error": f"Error learning logs: {e}"}

async def run_ping_command_async(device_name: str, command: str) -> Dict[str, Any]:
    """Execute a ping command on a device using SSHPass."""
    try:
        if not command.lower().strip().startswith("ping"):
            return {"status": "error", "error": f"Command '{command}' is not a 'ping' command."}
        
        params = _get_device_params(device_name)
        logger.info(f"[{device_name}] Executing ping via SSHPass: '{command}'")
        res = await direct_ssh_execute(params['ip'], params['username'], params['password'], command)
        
        if res['status'] != 'success':
            return {"status": "error", "error": res.get('error_output')}

        parsed_output = _offline_parse_sync(params['device_obj'], command, res['output'])
        status = "completed" if isinstance(parsed_output, dict) else "completed_raw"
        
        return {"status": status, "device": device_name, "output": parsed_output}
    except Exception as e:
        logger.error(f"Error in run_ping_command_async: {e}", exc_info=True)
        return {"status": "error", "error": f"Ping execution error: {e}"}

async def run_linux_command_async(device_name: str, command: str) -> Dict[str, Any]:
    """Execute a Linux command on a device using SSHPass."""
    try:
        params = _get_device_params(device_name)
        logger.info(f"[{device_name}] Executing Linux command via SSHPass: '{command}'")
        res = await direct_ssh_execute(params['ip'], params['username'], params['password'], command)
        
        if res['status'] != 'success':
            return {"status": "error", "error": res.get('error_output')}
            
        return {"status": "completed", "device": device_name, "output": res['output']}
    except Exception as e:
        logger.error(f"Error in run_linux_command_async: {e}", exc_info=True)
        return {"status": "error", "error": f"Linux command execution error: {e}"}

async def run_adhoc_command_async(ip: str, os_type: str, username: str, password: str, command: str) -> Dict[str, Any]:
    """Execute a command on a dynamically provided device using SSHPass."""
    try:
        logger.info(f"Executing ad-hoc command via SSHPass to {ip}: '{command}'")
        res = await direct_ssh_execute(ip, username, password, command)
        
        if res['status'] != 'success':
            return {"status": "error", "error": res.get('error_output')}
            
        # Try a generic device object for offline parsing if possible
        temp_tb = Testbed('temp')
        temp_dev = Device('temp_dev', testbed=temp_tb, os=os_type, type='router')
        parsed = _offline_parse_sync(temp_dev, command, res['output'])
        
        return {"status": "success", "device_ip": ip, "output": parsed}
    except Exception as e:
        logger.error(f"Error in run_adhoc_command_async: {e}", exc_info=True)
        return {"status": "error", "error": f"Ad-hoc execution error: {e}"}

async def direct_ssh_execute(ip: str, username: str, password: str, commands: str) -> Dict[str, Any]:
    """Execute raw commands via SSHPass using a temporary file and synchronous subprocess."""
    ip_str = str(ip)
    tmp_filename = f"ssh_cmds_{ip_str.replace('.', '_')}_{os.getpid()}.txt"
    tmp_path = os.path.join("/tmp", tmp_filename)
    try:
        # 1. Write commands to temp file
        with open(tmp_path, 'w') as f:
            f.write(f"{commands}\nexit\n")
        
        # 2. Build the command string
        full_shell_cmd = (
            f"sshpass -p '{password}' ssh -o StrictHostKeyChecking=no "
            f"-o UserKnownHostsFile=/dev/null -T {username}@{ip_str} < {tmp_path}"
        )
        
        logger.info(f"Executing [sshpass ... < {tmp_filename}] for {ip_str}...")
        
        # Using run_in_executor with synchronous subprocess.run for better PTY/Redirection stability
        def _sync_run():
            import subprocess
            return subprocess.run(
                full_shell_cmd,
                shell=True,
                capture_output=True,
                text=True
            )

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, _sync_run)
        
        cleaned_stdout = clean_output(result.stdout)
        cleaned_stderr = clean_output(result.stderr)
        
        if result.returncode == 0:
            logger.info(f"Direct SSH to {ip_str} succeeded (Return code: {result.returncode})")
            return {
                "status": "success",
                "device_ip": ip_str,
                "output": cleaned_stdout,
                "error_output": cleaned_stderr,
                "return_code": result.returncode
            }
        
        logger.error(f"Direct SSH to {ip_str} failed. Return code: {result.returncode}")
        logger.error(f"Error output: {cleaned_stderr}")
        return {
            "status": "error",
            "device_ip": ip_str,
            "output": cleaned_stdout,
            "error": cleaned_stderr,
            "return_code": result.returncode
        }
            
    except Exception as e:
        logger.error(f"Error in direct_ssh_execute: {e}", exc_info=True)
        return {"status": "error", "error": str(e)}
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except:
                pass

async def add_device_to_testbed_async(device_name: str, ip: str, os_type: str, username: str, password: str, protocol: str, port: int = 22) -> Dict[str, Any]:
    """Add a new device to the existing testbed.yaml."""
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, partial(_add_device_to_testbed, device_name, ip, os_type, username, password, protocol, port))
        return result
    except Exception as e:
        logger.error(f"Error in add_device_to_testbed_async: {e}", exc_info=True)
        return {"status": "error", "error": f"Testbed update error: {e}"}

def _add_device_to_testbed(device_name: str, ip: str, os_type: str, username: str, password: str, protocol: str, port: int = 22) -> Dict[str, Any]:
    """Synchronous helper for updating testbed.yaml."""
    try:
        logger.info(f"Adding new device {device_name} to {TESTBED_PATH}...")
        with open(TESTBED_PATH, 'r') as f:
            tb_data = yaml.safe_load(f) or {}

        if 'devices' not in tb_data:
            tb_data['devices'] = {}

        if device_name in tb_data['devices']:
            return {"status": "error", "error": f"Device '{device_name}' already exists in the testbed."}

        tb_data['devices'][device_name] = {
            "alias": device_name,
            "type": "router",
            "os": os_type,
            "connections": {
                "cli": {"protocol": protocol, "ip": ip, "port": port, "arguments": {"connection_timeout": 60}}
            },
            "credentials": {"default": {"username": username, "password": password}}
        }

        with open(TESTBED_PATH, 'w') as f:
            yaml.dump(tb_data, f, default_flow_style=False, sort_keys=False)

        return {"status": "success", "message": f"Device '{device_name}' successfully added to testbed."}
    except Exception as e:
        logger.error(f"Error updating testbed.yaml: {e}", exc_info=True)
        return {"status": "error", "error": str(e)}

async def verify_bgp_convergence_async(device_name: str, address_family: BGPAddressFamily = BGPAddressFamily.IPV4_UNICAST, max_retries: int = 12, sleep_interval: int = 5) -> Dict[str, Any]:
    """Wait for BGP peering to converge by parsing state changes."""
    try:
        if address_family == BGPAddressFamily.IPV4_UNICAST:
            command = "show ip bgp summary"
        else:
            command = f"show bgp {address_family.value} summary"
            
        logger.info(f"[{device_name}] Polling '{command}' for convergence... (Max {max_retries * sleep_interval}s)")
        
        for attempt in range(max_retries):
            result = await run_show_command_async(device_name, command)
            if result.get("status") != "completed":
                return {"status": "error", "error": f"Failed to parse BGP output: {result.get('error')}"}
                
            parsed_data = result.get("output", {})
            is_converged = True
            
            vrfs = parsed_data.get('vrf', {})
            if not vrfs:
                 is_converged = False
                 
            for vrf_name, vrf_data in vrfs.items():
                neighbors = vrf_data.get('neighbor', {})
                if not neighbors:
                    is_converged = False
                for peer_ip, peer_data in neighbors.items():
                    af_data = peer_data.get('address_family', {})
                    for af, stats in af_data.items():
                        state_pfxrcd = str(stats.get('state_pfxrcd', 'Idle')).strip()
                        if not state_pfxrcd.isdigit():
                            is_converged = False
                            
            if is_converged and vrfs:
                return {
                    "status": "success", 
                    "message": f"BGP {address_family.value} converged successfully in attempt {attempt+1}.", 
                    "parsed_data": parsed_data
                }
                
            await asyncio.sleep(sleep_interval)
            
        return {"status": "timeout", "error": f"Timeout: BGP did not converge within {max_retries * sleep_interval} seconds."}
        
    except Exception as e:
        logger.error(f"Error checking BGP convergence: {e}", exc_info=True)
        return {"status": "error", "error": str(e)}

# --- Future SSoT Integration (Stub) ---
async def fetch_device_from_ssot_async(device_name: str) -> Dict[str, Any]:
    """
    [FUTURE DEVELOPMENT] Fetch device connection details from an SSoT (e.g., NetBox, CMDB).
    """
    # TODO: Implement actual API call to your SSoT here using httpx or aiohttp
    # Expected return: {"ip": "...", "os_type": "...", "username": "...", "password": "...", "protocol": "ssh"}
    logger.info(f"Placeholder: Fetching details for {device_name} from SSoT...")
    raise NotImplementedError("SSoT integration is not yet implemented.")

# --- Initialize FastMCP ---
mcp = FastMCP("pyATS Network Automation Server")

# --- Define Tools ---

@mcp.tool()
async def pyats_list_learnable_features() -> str:
    """
    List all platform-neutral features that Genie can 'learn'.
    Use this to find which features are available for pyats_learn_feature.
    Common features: ospf, bgp, interface, routing, lldp, vrf.
    """
    try:
        from genie.ops.utils import get_ops_list
        # Genie maintains a list of searchable ops/features
        features = ["ospf", "bgp", "interface", "routing", "isis", "lldp", "vrf", "vlan", "mcast", "vxlan", "segment-routing", "srv6"]
        return json.dumps({"available_features": features, "hint": "Use pyats_discover_json_structure to see data layout."}, indent=2)
    except Exception as e:
        return json.dumps(["ospf", "bgp", "interface", "routing", "isis", "lldp", "vrf"], indent=2)

@mcp.tool()
async def pyats_discover_json_structure(
    device_name: str,
    command: str = "",
    feature: str = ""
) -> str:
    """
    Discover the JSON hierarchy (keys only) of a command or feature.
    **CRITICAL FOR PERFORMANCE**: Use this first to find the exact 'filter_query' path.
    By mapping the structure before fetching data, you avoid huge payloads and 
    drastically reduce latency (up to 90% faster) compared to full data transfers.

    Args:
        device_name: Name of the device in the testbed
        command:     (Optional) The show command to map (e.g., 'show interfaces')
        feature:     (Optional) The feature to map (e.g., 'ospf')
    """
    try:
        data = {}
        if command:
            data = await run_show_command_async(device_name, command)
        elif feature:
            data = await learn_feature_async(device_name, feature)
        else:
            return "Error: Provide either a command or a feature."

        if not isinstance(data, dict):
            return f"Result is not a structure: {type(data).__name__}"

        def get_keys_only(d):
            if isinstance(d, dict):
                return {k: get_keys_only(v) for k, v in d.items()}
            elif isinstance(d, list):
                if d and isinstance(d[0], (dict, list)):
                    return [get_keys_only(d[0])]
                return ["<list_values>"]
            else:
                return "<value>"

        structure_map = get_keys_only(data)
        return json.dumps(structure_map, indent=2)
    except Exception as e:
        logger.error(f"Error in pyats_discover_json_structure: {e}", exc_info=True)
        return json.dumps({"status": "error", "error": str(e)}, indent=2)

@mcp.tool()
async def pyats_run_show_command(
    device_name: str,
    command: str,
    use_genie: bool = True,
    filter_query: str = ""
) -> str:
    """
    Execute a Cisco IOS/NX-OS 'show' command.
    Uses Genie parsing by default to return structured JSON.
    Use 'filter_query' (Genie Dq) to select specific data and save tokens.
    Example filter_query: 'info.interfaces.GigabitEthernet1.ip_address'

    Args:
        device_name:  Name of the device in the testbed
        command:      The show command to execute
        use_genie:    Whether to parse output into JSON (default: True)
        filter_query: (Optional) Genie Dq query to filter the JSON output
    """
    try:
        if use_genie:
            result = await run_show_command_async(device_name, command)
            # Apply Dq filtering if requested and result is a dict
            if filter_query and isinstance(result, dict):
                from genie.utils.dq import Dq
                result = Dq(result).query(filter_query)
            return json.dumps(result, indent=2)
        else:
            # Raw output - Dq cannot be applied to raw string
            raw_output = await run_show_command_raw_async(device_name, command)
            return raw_output
    except Exception as e:
        logger.error(f"Error in pyats_run_show_command: {e}", exc_info=True)
        return json.dumps({"status": "error", "error": str(e)}, indent=2)

@mcp.tool()
async def pyats_configure_device(device_name: str, config_commands: str) -> str:
    """
    Apply configuration commands to a Cisco IOS/NX-OS device.
    
    [CRITICAL BGP CONFIGURATION RULES]
    Whenever generating BGP configurations, you MUST strictly adhere to this standard:
    1. ALWAYS disable default IPv4 behavior globally by including 'no bgp default ipv4-unicast'.
    2. ALWAYS explicitly enter 'address-family ipv4' block and explicitly 'activate' standard BGP neighbors.
    
    Args:
        device_name: The name of the device in the testbed
        config_commands: Configuration commands to apply (can be multi-line)
    
    Returns:
        JSON string containing the configuration result
    """
    try:
        result = await apply_device_configuration_async(device_name, config_commands)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error in pyats_configure_device: {e}", exc_info=True)
        return json.dumps({"status": "error", "error": str(e)}, indent=2)

@mcp.tool()
async def pyats_show_running_config(device_name: str) -> str:
    """
    Retrieve the running configuration from a Cisco IOS/NX-OS device.
    
    Args:
        device_name: The name of the device in the testbed
    
    Returns:
        JSON string containing the running configuration
    """
    try:
        result = await execute_learn_config_async(device_name)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error in pyats_show_running_config: {e}", exc_info=True)
        return json.dumps({"status": "error", "error": str(e)}, indent=2)

@mcp.tool()
async def pyats_show_logging(device_name: str) -> str:
    """
    Retrieve recent system logs from a Cisco IOS/NX-OS device.
    
    Args:
        device_name: The name of the device in the testbed
    
    Returns:
        JSON string containing the recent logs
    """
    try:
        result = await execute_learn_logging_async(device_name)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error in pyats_show_logging: {e}", exc_info=True)
        return json.dumps({"status": "error", "error": str(e)}, indent=2)

@mcp.tool()
async def pyats_ping_from_network_device(device_name: str, command: str) -> str:
    """
    Execute a ping command from a Cisco IOS/NX-OS device.
    
    Args:
        device_name: The name of the device in the testbed
        command: The ping command to execute (e.g., 'ping 8.8.8.8')
    
    Returns:
        JSON string containing the ping results
    """
    try:
        result = await run_ping_command_async(device_name, command)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error in pyats_ping_from_network_device: {e}", exc_info=True)
        return json.dumps({"status": "error", "error": str(e)}, indent=2)

@mcp.tool()
async def pyats_run_linux_command(device_name: str, command: str) -> str:
    """
    Execute a Linux command on a specified device.
    
    Args:
        device_name: The name of the Linux device in the testbed
        command: The Linux command to execute (e.g., 'ifconfig', 'ps -ef')
    
    Returns:
        JSON string containing the command output
    """
    try:
        result = await run_linux_command_async(device_name, command)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error in pyats_run_linux_command: {e}", exc_info=True)
        return json.dumps({"status": "error", "error": str(e)}, indent=2)

@mcp.tool()
async def pyats_learn_feature(
    device_name: str,
    feature: str,
    filter_query: str = ""
) -> str:
    """
    Learn a network feature (e.g., 'ospf', 'bgp', 'interface') as a structured database.
    This provides a deep, vendor-neutral JSON model of the device state.
    ALWAYS use 'filter_query' (Genie Dq) for large features to save tokens.

    Args:
        device_name:  The name of the device in the testbed
        feature:      The feature to learn (e.g., 'ospf', 'isis', 'routing')
        filter_query: (Optional) Genie Dq query. E.g., 'info.vrf[default].neighbor'
    """
    try:
        result = await learn_feature_async(device_name, feature)
        # Apply Dq filtering if requested
        if filter_query and isinstance(result, dict):
            from genie.utils.dq import Dq
            result = Dq(result).query(filter_query)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error in pyats_learn_feature: {e}", exc_info=True)
        return json.dumps({"status": "error", "error": str(e)}, indent=2)

@mcp.tool()
async def pyats_run_command_adhoc(ip: str, os_type: str, username: str, password: str, command: str) -> str:
    """
    (Ad-hoc) Execute a command on a dynamically provided device without needing a testbed.yaml file.
    
    Args:
        ip: IP address of the device
        os_type: OS type (e.g., 'iosxe', 'nxos', 'linux')
        username: SSH username
        password: SSH password
        command: The command to execute
    """
    try:
        result = await run_adhoc_command_async(ip, os_type, username, password, command)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error in pyats_run_command_adhoc: {e}", exc_info=True)
        return json.dumps({"status": "error", "error": str(e)}, indent=2)

@mcp.tool()
async def direct_ssh_configure(ip: str, username: str, password: str, config_commands: str) -> str:
    """
    Apply configuration commands extremely quickly using raw SSH (sshpass), bypassing pyATS state machines.
    Useful when pyats_configure_device fails due to generic 'any state' errors or when speed is critical.

    [CRITICAL BGP CONFIGURATION RULES]
    Whenever generating BGP configurations, you MUST strictly adhere to this standard:
    1. ALWAYS disable default IPv4 behavior globally by including 'no bgp default ipv4-unicast'.
    2. ALWAYS explicitly enter 'address-family ipv4' block and explicitly 'activate' standard BGP neighbors.
    
    Args:
        ip: IP address of the device
        username: SSH username
        password: SSH password
        config_commands: Configuration commands to apply (can be multi-line string)
        
    Returns:
        JSON string containing the raw terminal output
    """
    try:
        result = await direct_ssh_execute(ip, username, password, config_commands)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error in direct_ssh_configure: {e}", exc_info=True)
        return json.dumps({"status": "error", "error": str(e)}, indent=2)

@mcp.tool()
async def add_device_to_testbed(device_name: str, ip: str, os_type: str, username: str, password: str, protocol: str = "ssh", port: int = 22) -> str:
    """
    Dynamically add a new device to the active testbed.yaml file for future connections.
    If the user requests to add a device but missing any of the required arguments (device_name, ip, os_type, username, password),
    you MUST politely ask the user to provide the missing information in the conversation.
    
    Args:
        device_name: Name of the new device to add
        ip: IP address of the device
        os_type: OS type (e.g., 'iosxe', 'nxos', 'linux')
        username: SSH username
        password: SSH password
        protocol: Connection protocol (default: 'ssh')
        port: SSH port (default: 22)
    """
    try:
        result = await add_device_to_testbed_async(device_name, ip, os_type, username, password, protocol, port)
        # Reload the loader with the updated testbed path to refresh cache if needed globally
        loader.load(TESTBED_PATH)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error in add_device_to_testbed: {e}", exc_info=True)
        return json.dumps({"status": "error", "error": str(e)}, indent=2)

@mcp.tool()
async def sync_device_from_ssot(device_name: str) -> str:
    """
    [FUTURE DEVELOPMENT / WIP]
    Automatically resolve device info (IP, OS, credentials) from a Single Source of Truth (SSoT) 
    using only the device name, and add it to the testbed without asking the user.
    
    Args:
        device_name: Name of the device to lookup in the SSoT
    """
    try:
        # 1. Fetch from SSoT
        # info = await fetch_device_from_ssot_async(device_name)
        # 2. Add to testbed and connect
        # result = await add_device_to_testbed_async(device_name, info['ip'], info['os_type'], info['username'], info['password'], info['protocol'])
        # return json.dumps(result, indent=2)
        return json.dumps({"status": "pending", "message": "SSoT integration is planned for a future release. For now, please use 'add_device_to_testbed' manually."}, indent=2)
    except Exception as e:
        logger.error(f"Error in sync_device_from_ssot: {e}", exc_info=True)
        return json.dumps({"status": "error", "error": str(e)}, indent=2)

@mcp.tool()
async def pyats_get_route_detail(device_name: str, prefix: str, vrf: Optional[str] = None) -> str:
    """
    Retrieve detailed routing information for a specific IP prefix or address.
    This is highly token-efficient as it only fetches data for the requested route.
    
    Args:
        device_name: The name of the device in the testbed
        prefix: The IP address or prefix to look up (e.g., '10.1.1.1' or '192.168.1.0/24')
        vrf: (Optional) The VRF name to check (e.g., 'MGMT')
    
    Returns:
        JSON string containing the parsed routing details for the specific prefix.
    """
    # Validate prefix to prevent command injection
    if not re.match(r'^[\d\./]+$', prefix):
        return json.dumps({"status": "error", "error": f"Invalid prefix format: {prefix}"}, indent=2)

    command = f"show ip route {prefix}"
    if vrf:
        if not re.match(r'^[a-zA-Z0-9_-]+$', vrf):
            return json.dumps({"status": "error", "error": f"Invalid VRF name: {vrf}"}, indent=2)
        command = f"show ip route vrf {vrf} {prefix}"

    try:
        # Re-use the existing run_show_command_async which perfectly handles Genie parsing
        result = await run_show_command_async(device_name, command)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error in pyats_get_route_detail: {e}", exc_info=True)
        return json.dumps({"status": "error", "error": str(e)}, indent=2)

@mcp.tool()
async def pyats_get_ospf_lsa_detail(device_name: str, lsa_type: str, lsa_id: str, vrf: Optional[str] = None, process_id: Optional[str] = None) -> str:
    """
    Retrieve detailed OSPF LSA (Link State Advertisement) information for a specific LSA type and ID.
    This allows pinpoint troubleshooting of OSPF topology without consuming massive tokens by learning the whole database.
    
    Args:
        device_name: The name of the device in the testbed
        lsa_type: The type of LSA to query (e.g., 'router', 'network', 'summary', 'asbr-summary', 'external')
        lsa_id: The Link State ID (usually an IP address, e.g., '192.168.1.1')
        vrf: (Optional) The VRF name to check (e.g., 'MGMT')
        process_id: (Optional) The OSPF process ID
    
    Returns:
        JSON string containing the parsed OSPF LSA details.
    """
    valid_lsa_types = ['router', 'network', 'summary', 'asbr-summary', 'external']
    if lsa_type.lower() not in valid_lsa_types:
        return json.dumps({"status": "error", "error": f"Invalid LSA type. Must be one of: {', '.join(valid_lsa_types)}"}, indent=2)

    if not re.match(r'^[\d\.]+$', lsa_id):
        return json.dumps({"status": "error", "error": f"Invalid LSA ID format: {lsa_id}"}, indent=2)

    command_parts = ["show ip ospf"]
    if vrf:
        if not re.match(r'^[a-zA-Z0-9_-]+$', vrf):
            return json.dumps({"status": "error", "error": f"Invalid VRF name: {vrf}"}, indent=2)
        command_parts.extend(["vrf", vrf])

    if process_id:
        if not str(process_id).isdigit():
            return json.dumps({"status": "error", "error": "Process ID must be numeric"}, indent=2)
        command_parts.append(str(process_id))

    command_parts.extend(["database", lsa_type.lower(), lsa_id])
    command = " ".join(command_parts)

    try:
        result = await run_show_command_async(device_name, command)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error in pyats_get_ospf_lsa_detail: {e}", exc_info=True)
        return json.dumps({"status": "error", "error": str(e)}, indent=2)

# ============================================================
# EVPN VXLAN & MVPN Provisioning Tools (Modern IOS-XE)
# Lifecycle-based: Day-1 Fabric → Day-2 L3VNI/L2VNI → MVPN
# ============================================================

@mcp.tool()
async def pyats_provision_evpn_fabric(
    device_name: str,
    peer_ip: str,
    router_id: str,
    asn: int = 65000,
    source_loopback: str = "Loopback0",
    role: str = "LF"
) -> str:
    """
    [Day-1] Provision the EVPN VXLAN fabric base configuration on a Modern IOS-XE device.
    Run this ONCE per VTEP before adding any VNIs.

    Requires 'router bgp {asn}' and 'bgp router-id {router_id}' to enable Auto RD/RT.
    Note: 'l2vpn evpn' router-id (L2VPN RID) is used as the base for Auto RD.

    Roles: LF (Leaf), SP (Spine), BL (Border Leaf), BGW (Border Gateway).
    Default is LF. Spine can also act as BL. Sets up:
    - BGP L2VPN EVPN address family with neighbor peering
    - BGP router-id and global disable of default ipv4 unicast
    - Note: NVE and Global EVPN settings are now handled in the L2VNI/L3VNI tools for service self-containment.

    Args:
        device_name:       Name of the device in the testbed
        peer_ip:           Remote VTEP / BGP EVPN neighbor IP
        router_id:         BGP Router-ID IP (Required for Auto RD generation)
        asn:               BGP Autonomous System Number (default: 65000, for Auto RT)
    """
    try:
        config = f"""
router bgp {asn}
  bgp router-id {router_id}
  no bgp default ipv4-unicast
  address-family l2vpn evpn
    neighbor {peer_ip} activate
    neighbor {peer_ip} send-community both
"""

        result = await apply_device_configuration_async(device_name, config)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error in pyats_provision_evpn_fabric: {e}", exc_info=True)
        return json.dumps({"status": "error", "error": str(e)}, indent=2)



@mcp.tool()
async def pyats_add_l3vni(
    device_name: str,
    vrf_name: str,
    l3vni: int,
    l3_vlan: int,
    peer_ip: str = "",
    asn: int = 65000,
    role: str = "LF",
    vrf_rd: str = "",
    vrf_rt: str = ""
) -> str:
    """
    [Day-2] Add a Layer-3 VNI (L3VNI) to an existing EVPN fabric on Modern IOS-XE.
    Enables inter-subnet routing (Type-5 IP Prefix routes) for a tenant VRF.

    L3VNI is directly mapped to the VRF, so a separate L2VPN EVI instance configuration is not required.

    Roles & Stitching:
    If the role is BL (Border Leaf) or BGW (Border Gateway), route-targets 
    will include the 'stitching' keyword. Note: Spine (SP) can also act as BL/BGW.

    Args:
        device_name:  Name of the device in the testbed
        vrf_name:     VRF name for this tenant (e.g., 'VRF_TENANT_A')
        l3vni:        Layer-3 VNI ID (e.g., 20000)
        l3_vlan:      VLAN ID associated with the L3VNI (e.g., 20)
        peer_ip:      Remote VTEP IP — Unused in L3VNI config, kept for API compatibility.
        asn:          BGP AS Number (Required for RT stitching calculation)
        role:         Device role: LF, SP, BL, BGW (default: LF)
        vrf_rd:       (Optional) Explicit RD for VRF (e.g., '1:100'). Overrides auto-RD.
        vrf_rt:       (Optional) Explicit RT for VRF (e.g., '65000:20000'). Overrides auto-RT.
    """
    try:
        # Build VRF RD/RT Override lines
        vrf_override_lines = ""
        if vrf_rd:
            vrf_override_lines += f"  rd {vrf_rd}\n"
        
        if vrf_rt:
            vrf_override_lines += f"  route-target export {vrf_rt}\n"
            vrf_override_lines += f"  route-target import {vrf_rt}\n"

        # Stitching logic for Border devices
        role_upper = role.upper()
        stitching_config = ""
        if "BL" in role_upper or "BGW" in role_upper:
            # Calculate RT from formula <ASN>:<VNI>
            rt_val = f"{asn}:{l3vni}"
            stitching_config = f"  route-target export {rt_val} stitching\n  route-target import {rt_val} stitching\n"

        config = f"""
vrf definition {vrf_name}
  vnid {l3vni}
{vrf_override_lines}  address-family ipv4
{stitching_config}  exit-address-family

vlan {l3_vlan}
vlan configuration {l3_vlan}
  member vni {l3vni}

interface Vlan{l3_vlan}
  vrf forwarding {vrf_name}
  ip unnumbered Loopback0
  no autostate
  no shutdown

interface nve 1
  member vni {l3vni} vrf {vrf_name}

router bgp {asn}
  address-family ipv4 vrf {vrf_name}
    advertise l2vpn evpn
    redistribute connected
"""
        result = await apply_device_configuration_async(device_name, config)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error in pyats_add_l3vni: {e}", exc_info=True)
        return json.dumps({"status": "error", "error": str(e)}, indent=2)



@mcp.tool()
async def pyats_add_l2vni(
    device_name: str,
    l2vni: int,
    l2_vlan: int,
    l2_evi: int,
    peer_ip: str = "",
    anycast_gw_ip: str = "",
    anycast_gw_mask: str = "255.255.255.0",
    anycast_gw_mac: str = "",
    evi_rd: str = "",
    evi_rt: str = "",
    role: str = "LF",
    replication_type: str = "ingress",
    source_loopback: str = "Loopback0"
) -> str:
    """
    [CRITICAL OPERATIONAL GUIDELINE]
    1. MAINTAIN AUTO: Always prioritize 'RD/RT Auto' configurations. Do NOT prematurely 
       switch to manual RD/RT just because a ping fails.
    2. CONTROL PLANE FIRST: Before testing with 'ping', you MUST continuously verify that 
       MAC addresses are being learned and advertised in the Control Plane (Type-2 routes).
       Use: 'show l2vpn evpn mac ip' to check for MAC/IP reachability.
    3. PING IS SECONDARY: Ping success depends on many factors (ARP, SVI state, etc.). 
       Control Plane convergence is the definitive proof of fabric health.

    Sets up:
    - Global 'vrf rd-auto' and 'l2vpn evpn' (RT auto vni)
    - Interface NVE 1 base (source-interface, host-reachability protocol bgp)
    - L2VPN EVPN Instance (EVI) for L2VNI
    - VLAN-to-VNI mapping (member evpn-instance)
    - L2VNI SVI: optional anycast gateway IP/MAC
    - NVE 1: member vni <l2vni> with ingress-replication discovery

    Args:
        device_name:      Name of the device in the testbed
        l2vni:            Layer-2 VNI ID (e.g., 10000)
        l2_vlan:          VLAN ID for this L2VNI (e.g., 10)
        l2_evi:           EVPN Instance ID for this VLAN (e.g., 101)
        peer_ip:          (Optional) Remote VTEP IP. Unused if BGP discovery is active.
        anycast_gw_ip:    (Optional) Anycast gateway IP
        anycast_gw_mask:  Subnet mask for the anycast gateway
        anycast_gw_mac:   (Optional) Shared gateway MAC
        evi_rd:           (Optional) Explicit RD for EVI. Overrides auto-RD.
        evi_rt:           (Optional) Explicit RT for EVI. Overrides auto-RT.
        role:             Device role: LF, SP, BL, BGW (default: LF)
        replication_type: BUM replication method: 'ingress' or 'static' (default: ingress)
        source_loopback:  Loopback interface used for EVPN router-id (default: Loopback0)
    """
    try:
        if anycast_gw_ip:
            svi_ip_line = f" ip address {anycast_gw_ip} {anycast_gw_mask}"
            svi_mac_line = f" mac-address {anycast_gw_mac}\n" if anycast_gw_mac else ""
            evi_gw_line = "  default-gateway advertise\n" if not anycast_gw_mac else ""
        else:
            svi_ip_line = " no ip address"
            svi_mac_line = ""
            evi_gw_line = ""

        # Build EVI configuration lines
        # On Modern IOS-XE with global 'route-target auto vni', the instance config is minimal
        evi_config_lines = ""
        if evi_rd:
            evi_config_lines += f"  rd {evi_rd}\n"
        
        # If user explicitly provides RT, we add it, otherwise we rely on global auto
        if evi_rt and evi_rt != "auto":
            evi_config_lines += f"  route-target export {evi_rt}\n"
            evi_config_lines += f"  route-target import {evi_rt}\n"

        config = f"""
vrf rd-auto
!
l2vpn evpn
  replication-type {replication_type}
  router-id {source_loopback}
  route-target auto vni
!
interface nve 1
  no ip address
  no shutdown
  source-interface {source_loopback}
  host-reachability protocol bgp
!
l2vpn evpn instance {l2_evi} vlan-based
  encapsulation vxlan
{evi_config_lines}{evi_gw_line}
vlan {l2_vlan}
vlan configuration {l2_vlan}
  member evpn-instance {l2_evi} vni {l2vni}

interface Vlan{l2_vlan}
  no shutdown
{svi_mac_line}{svi_ip_line}

interface nve 1
  member vni {l2vni}
    ingress-replication
"""
        result = await apply_device_configuration_async(device_name, config)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error in pyats_add_l2vni: {e}", exc_info=True)
        return json.dumps({"status": "error", "error": str(e)}, indent=2)



@mcp.tool()
async def pyats_provision_mvpn(
    device_name: str,
    vrf_name: str,
    peer_ip: str,
    mdt_default_group: str,
    rp_address: str,
    asn: int = 65000,
    mdt_data_group: str = "",
    mdt_data_mask: str = "255.255.255.0",
    mdt_data_threshold_kbps: int = 0,
    source_loopback: str = "Loopback0"
) -> str:
    """
    [Day-1/Day-2] Provision Multicast VPN (NG-MVPN) for a tenant VRF on Modern IOS-XE.
    Uses BGP as the MVPN signaling protocol (RFC 6514 / NG-MVPN).

    NG-MVPN on IOS-XE uses:
    - MDT (Multicast Distribution Tree) for data-plane transport
    - BGP MVPN address family for C-multicast route signaling (Type-1 ~ Type-7)
    - PIM Sparse-Mode within the VRF for customer multicast

    Sets up:
    - VRF with MDT default group (shared tree for control) and optional MDT data groups
    - BGP: vpnv4 unicast + ipv4 mvpn address families
    - PIM RP configuration within the VRF

    Args:
        device_name:             Name of the device in the testbed
        vrf_name:                Tenant VRF name (e.g., 'VRF_MCAST')
        peer_ip:                 BGP MVPN peer IP (PE-PE or RR)
        mdt_default_group:       MDT default multicast group (e.g., '232.1.1.1')
        rp_address:              PIM Rendezvous Point IP within the VRF
        asn:                     BGP Autonomous System Number (default: 65000)
        mdt_data_group:          (Optional) MDT data group base address for high-traffic flows
        mdt_data_mask:           Subnet mask for MDT data group range (default: 255.255.255.0)
        mdt_data_threshold_kbps: Traffic threshold in kbps to switch to MDT data tree (default: 0)
        source_loopback:         Loopback interface for MDT source (default: Loopback0)
    """
    try:
        # Optional MDT data group config
        mdt_data_lines = ""
        if mdt_data_group:
            mdt_data_lines = (
                f"  mdt data {mdt_data_group} {mdt_data_mask}"
                + (f" threshold {mdt_data_threshold_kbps}" if mdt_data_threshold_kbps else "")
                + "\n"
            )

        config = f"""
vrf definition {vrf_name}
 address-family ipv4
  mdt default {mdt_default_group}
{mdt_data_lines}  mdt auto-discovery bgp
  mdt overlay use-bgp
 exit-address-family

ip pim vrf {vrf_name} rp-address {rp_address}
ip pim vrf {vrf_name} ssm range 232.0.0.0/8

router bgp {asn}
 address-family vpnv4
  neighbor {peer_ip} activate
  neighbor {peer_ip} send-community both
 address-family ipv4 mvpn vrf {vrf_name}
  neighbor {peer_ip} activate
  neighbor {peer_ip} send-community both
"""
        result = await apply_device_configuration_async(device_name, config)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error in pyats_provision_mvpn: {e}", exc_info=True)
        return json.dumps({"status": "error", "error": str(e)}, indent=2)


# ============================================================
# OSPF Provisioning Tools (IOS-XE)
# Lifecycle-based: Day-1 Process → Day-2 Interfaces/Areas → Verify
# ============================================================

@mcp.tool()
async def pyats_provision_ospf(
    device_name: str,
    router_id: str,
    process_id: int = 1,
    version: int = 2,
    default_passive: bool = False,
    log_adjacency: bool = True,
    spf_init_ms: int = 100,
    spf_hold_ms: int = 1000,
    spf_max_ms: int = 10000
) -> str:
    """
    [Day-1] Initialize an OSPF process on an IOS-XE device.
    Run this ONCE per device before adding any interfaces or areas.

    Supports OSPFv2 (IPv4) and OSPFv3 with Address-Family mode (version=3).

    Args:
        device_name:     Name of the device in the testbed
        router_id:       OSPF Router ID (must be unique, e.g., '1.1.1.1')
        process_id:      OSPF process ID (default: 1)
        version:         OSPF version: 2 (OSPFv2/IPv4) or 3 (OSPFv3 Address-Family, default: 2)
        default_passive: Set all interfaces passive by default (default: False)
        log_adjacency:   Log adjacency state changes detail (default: True)
        spf_init_ms:     SPF initial delay in ms (default: 100)
        spf_hold_ms:     SPF hold time in ms (default: 1000)
        spf_max_ms:      SPF maximum delay in ms (default: 10000)
    """
    try:
        passive_line = "\n passive-interface default" if default_passive else ""
        log_line = "\n log-adjacency-changes detail" if log_adjacency else ""

        if version == 3:
            config = f"""
router ospfv3 {process_id}
 router-id {router_id}{log_line}{passive_line}
 timers throttle spf {spf_init_ms} {spf_hold_ms} {spf_max_ms}
 address-family ipv4 unicast
  router-id {router_id}
 exit-address-family
 address-family ipv6 unicast
  router-id {router_id}
 exit-address-family
"""
        else:
            config = f"""
router ospf {process_id}
 router-id {router_id}{log_line}{passive_line}
 timers throttle spf {spf_init_ms} {spf_hold_ms} {spf_max_ms}
"""
        result = await apply_device_configuration_async(device_name, config)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error in pyats_provision_ospf: {e}", exc_info=True)
        return json.dumps({"status": "error", "error": str(e)}, indent=2)


@mcp.tool()
async def pyats_ospf_add_interface(
    device_name: str,
    interface: str,
    area: str,
    process_id: int = 1,
    version: int = 2,
    network_type: str = "",
    cost: int = 0,
    priority: int = -1,
    auth_type: str = "",
    auth_key: str = "",
    auth_key_id: int = 1,
    passive: bool = False,
    bfd: bool = False
) -> str:
    """
    [Day-2] Add an interface to OSPF using IOS-XE interface-level commands.
    Uses 'ip ospf <pid> area <area>' style — not the legacy 'network' statement.

    Args:
        device_name:  Name of the device in the testbed
        interface:    Interface name (e.g., 'GigabitEthernet0/1', 'Loopback0', 'Vlan10')
        area:         OSPF area ID (e.g., '0', '0.0.0.0', '10')
        process_id:   OSPF process ID (default: 1)
        version:      OSPF version: 2 or 3 (default: 2)
        network_type: 'point-to-point', 'broadcast', 'non-broadcast', 'point-to-multipoint'
        cost:         OSPF interface cost (0 = IOS default)
        priority:     DR/BDR priority 0-255 (-1 = do not configure)
        auth_type:    Authentication: '' (none), 'md5', 'sha-hmac' (IOS-XE 16+)
        auth_key:     Authentication password/key
        auth_key_id:  Key ID for MD5/SHA-HMAC (default: 1)
        passive:      Make this interface passive (no OSPF hellos sent)
        bfd:          Enable BFD for fast failure detection on this interface
    """
    try:
        lines = [f"interface {interface}"]

        if version == 3:
            lines.append(f" ospfv3 {process_id} ipv4 area {area}")
            lines.append(f" ospfv3 {process_id} ipv6 area {area}")
            if network_type:
                lines.append(f" ospfv3 network {network_type}")
            if cost > 0:
                lines.append(f" ospfv3 cost {cost}")
            if priority >= 0:
                lines.append(f" ospfv3 priority {priority}")
            if bfd:
                lines.append(" ospfv3 bfd")
        else:
            lines.append(f" ip ospf {process_id} area {area}")
            if network_type:
                lines.append(f" ip ospf network {network_type}")
            if cost > 0:
                lines.append(f" ip ospf cost {cost}")
            if priority >= 0:
                lines.append(f" ip ospf priority {priority}")
            if auth_type.lower() == "md5":
                lines.append(" ip ospf authentication message-digest")
                lines.append(f" ip ospf message-digest-key {auth_key_id} md5 {auth_key}")
            elif auth_type.lower() == "sha-hmac":
                lines.append(f" ip ospf authentication key-chain OSPF_KEYS")
            if bfd:
                lines.append(" ip ospf bfd")

        passive_config = ""
        if passive:
            proc_cmd = f"ospfv3 {process_id}" if version == 3 else f"ospf {process_id}"
            passive_config = f"\nrouter {proc_cmd}\n passive-interface {interface}"

        config = "\n".join(lines) + "\n" + passive_config
        result = await apply_device_configuration_async(device_name, config)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error in pyats_ospf_add_interface: {e}", exc_info=True)
        return json.dumps({"status": "error", "error": str(e)}, indent=2)


@mcp.tool()
async def pyats_ospf_remove_interface(
    device_name: str,
    interface: str,
    process_id: int = 1,
    version: int = 2
) -> str:
    """
    [Day-2] Remove an interface from OSPF participation on IOS-XE.

    Args:
        device_name:  Name of the device in the testbed
        interface:    Interface name to remove from OSPF
        process_id:   OSPF process ID (default: 1)
        version:      OSPF version: 2 or 3 (default: 2)
    """
    try:
        if version == 3:
            config = f"""
interface {interface}
 no ospfv3 {process_id} ipv4 area
 no ospfv3 {process_id} ipv6 area
"""
        else:
            config = f"""
interface {interface}
 no ip ospf {process_id} area
"""
        result = await apply_device_configuration_async(device_name, config)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error in pyats_ospf_remove_interface: {e}", exc_info=True)
        return json.dumps({"status": "error", "error": str(e)}, indent=2)


@mcp.tool()
async def pyats_ospf_configure_area(
    device_name: str,
    area: str,
    area_type: str,
    process_id: int = 1,
    default_cost: int = 1,
    summary_address: str = "",
    summary_mask: str = ""
) -> str:
    """
    [Day-2] Configure OSPF area type (Stub/NSSA) and optional ABR summarization.

    Area types:
    - 'stub'         : No external (Type-5) LSAs; default route injected
    - 'totally_stub' : No external or inter-area (Type-3/4/5) LSAs — most restrictive
    - 'nssa'         : Allows Type-7 external LSAs (converted to Type-5 at ABR)
    - 'totally_nssa' : NSSA + blocks inter-area LSAs (Type-3/4)

    Note: Area 0 (backbone) cannot be stub or NSSA.

    Args:
        device_name:     Name of the device in the testbed
        area:            OSPF area ID (e.g., '10', '0.0.0.10')
        area_type:       'stub', 'totally_stub', 'nssa', 'totally_nssa'
        process_id:      OSPF process ID (default: 1)
        default_cost:    Cost of default route injected into stub/NSSA (default: 1)
        summary_address: (Optional) ABR route summarization prefix (e.g., '10.0.0.0')
        summary_mask:    Subnet mask for summarization (e.g., '255.255.0.0')
    """
    try:
        type_map = {
            "stub":         f" area {area} stub\n area {area} default-cost {default_cost}",
            "totally_stub": f" area {area} stub no-summary\n area {area} default-cost {default_cost}",
            "nssa":         f" area {area} nssa\n area {area} default-cost {default_cost}",
            "totally_nssa": f" area {area} nssa no-summary\n area {area} default-cost {default_cost}",
        }
        area_cmd = type_map.get(area_type.lower())
        if not area_cmd:
            return json.dumps({"status": "error", "error": f"Invalid area_type '{area_type}'. Must be: stub, totally_stub, nssa, totally_nssa"}, indent=2)

        summary_cmd = f"\n area {area} range {summary_address} {summary_mask}" if (summary_address and summary_mask) else ""

        config = f"""
router ospf {process_id}
{area_cmd}{summary_cmd}
"""
        result = await apply_device_configuration_async(device_name, config)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error in pyats_ospf_configure_area: {e}", exc_info=True)
        return json.dumps({"status": "error", "error": str(e)}, indent=2)


@mcp.tool()
async def pyats_ospf_redistribute(
    device_name: str,
    source_protocol: str,
    process_id: int = 1,
    source_process_id: str = "",
    metric: int = 20,
    metric_type: int = 2,
    subnets: bool = True,
    route_map: str = "",
    tag: int = 0
) -> str:
    """
    [Day-2] Configure route redistribution into OSPF on IOS-XE.

    Args:
        device_name:       Name of the device in the testbed
        source_protocol:   Source: 'connected', 'static', 'bgp', 'eigrp', 'rip', 'isis', 'ospf'
        process_id:        OSPF process ID to redistribute INTO (default: 1)
        source_process_id: Source protocol's ASN or process ID (required for bgp/eigrp/isis)
        metric:            Redistribution seed metric (default: 20)
        metric_type:       OSPF external metric type: 1 (E1, cumulative) or 2 (E2, flat, default)
        subnets:           Include subnets (default: True — strongly recommended)
        route_map:         (Optional) Route-map to filter or set attributes on redistributed routes
        tag:               (Optional) OSPF tag value for redistributed routes (0 = no tag)
    """
    try:
        valid = ["connected", "static", "bgp", "eigrp", "rip", "isis", "ospf"]
        if source_protocol.lower() not in valid:
            return json.dumps({"status": "error", "error": f"Invalid source_protocol. Must be one of: {', '.join(valid)}"}, indent=2)

        parts = [f"redistribute {source_protocol.lower()}"]
        if source_process_id:
            parts.append(str(source_process_id))
        if subnets:
            parts.append("subnets")
        parts.append(f"metric {metric}")
        parts.append(f"metric-type {metric_type}")
        if tag > 0:
            parts.append(f"tag {tag}")
        if route_map:
            parts.append(f"route-map {route_map}")

        config = f"""
router ospf {process_id}
 {' '.join(parts)}
"""
        result = await apply_device_configuration_async(device_name, config)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error in pyats_ospf_redistribute: {e}", exc_info=True)
        return json.dumps({"status": "error", "error": str(e)}, indent=2)


@mcp.tool()
async def pyats_verify_ospf(
    device_name: str,
    process_id: int = 1,
    expected_neighbor_count: int = 0
) -> str:
    """
    Verify OSPF operational state on an IOS-XE device.

    Runs:
    - show ip ospf neighbor        : Adjacency state (Full = healthy)
    - show ip ospf interface brief : Interface OSPF participation
    - show ip ospf database        : LSA database summary
    - show ip route ospf           : OSPF-learned routes in RIB

    Args:
        device_name:             Name of the device in the testbed
        process_id:              OSPF process ID to verify (default: 1)
        expected_neighbor_count: If > 0, validates that at least this many FULL neighbors exist
    """
    commands = [
        f"show ip ospf {process_id} neighbor",
        f"show ip ospf {process_id} interface brief",
        f"show ip ospf {process_id} database",
        "show ip route ospf",
    ]
    results = {}
    full_count = 0

    for cmd in commands:
        try:
            res = await run_show_command_async(device_name, cmd)
            results[cmd] = res
            if "neighbor" in cmd and res.get("status") == "completed":
                output = res.get("output", {})
                if isinstance(output, dict):
                    for intf_data in output.values():
                        if isinstance(intf_data, dict):
                            for nbr in intf_data.get("neighbors", {}).values():
                                if isinstance(nbr, dict) and nbr.get("state", "").upper().startswith("FULL"):
                                    full_count += 1
        except Exception as e:
            results[cmd] = {"status": "error", "error": str(e)}

    summary: dict = {"full_neighbor_count": full_count}
    if expected_neighbor_count > 0:
        summary["neighbor_check"] = "PASS" if full_count >= expected_neighbor_count else "FAIL"
        summary["expected"] = expected_neighbor_count
        summary["actual"] = full_count

    results["_summary"] = summary
    return json.dumps(results, indent=2)


@mcp.tool()
async def pyats_verify_bgp_convergence(device_name: str, address_family: BGPAddressFamily = BGPAddressFamily.IPV4_UNICAST, max_retries: int = 12, sleep_interval: int = 5) -> str:
    """
    Smart polling tool to wait for BGP peering convergence for a specific address family (e.g., ipv4 unicast, vpnv4 unicast all, l2vpn evpn).
    It repeatedly parses 'show bgp <af> summary' until the State/PfxRcd changes from Idle/Active to an integer limit.
    Use this tool IMMEDIATELY after configuring BGP to ensure convergence before checking routing tables.
    
    Args:
        device_name: The name of the device in the testbed
        address_family: The BGP address family to check (default: ipv4 unicast)
        max_retries: Max polling attempts (default: 12)
        sleep_interval: Seconds to wait between attempts (default: 5)
    """
    try:
        result = await verify_bgp_convergence_async(device_name, address_family, max_retries, sleep_interval)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error in pyats_verify_bgp_convergence: {e}", exc_info=True)
        return json.dumps({"status": "error", "error": str(e)}, indent=2)

@mcp.tool()
async def pyats_get_vxlan_status(device_name: str, nve_id: int = 1) -> str:
    """
    Retrieve and parse VXLAN/NVE status (VNI, Interface, Peers).
    Note: NVE is a virtual interface and might not be visible in some 'show' commands 
    unless it has been explicitly created/configured.
    
    Args:
        device_name: The name of the device in the testbed
        nve_id: The NVE interface ID (default: 1)
    """
    commands = [
        f"show nve interface nve {nve_id}",
        "show nve vni summary",
        "show nve vni"
    ]
    results = {}
    for cmd in commands:
        try:
            res = await run_show_command_async(device_name, cmd)
            results[cmd] = res
        except Exception as e:
            results[cmd] = {"status": "error", "error": f"Command '{cmd}' failed: {e}"}
            
    return json.dumps(results, indent=2)

# --- Main Function ---
if __name__ == "__main__":
    logger.info("🚀 Starting pyATS FastMCP Server...")
    mcp.run()