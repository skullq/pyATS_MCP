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
from pydantic import BaseModel, Field, ValidationError
from typing import Dict, Any, Optional
import asyncio
from functools import partial
import mcp.types as types
from mcp.server.fastmcp import FastMCP

# --- Basic Logging Setup ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("PyatsFastMCPServer")

# --- Load Environment Variables ---
load_dotenv()
# 환경 변수가 없으면 기본값으로 현재 디렉토리의 testbed.yaml 사용
TESTBED_PATH = os.getenv("PYATS_TESTBED_PATH", "testbed.yaml")

if not os.path.exists(TESTBED_PATH):
    logger.warning(f"⚠️ Testbed file not found at {TESTBED_PATH}. Creating an empty testbed. Users can add devices dynamically via chat.")
    # 빈 테스트베드 파일 자동 초기화
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

# --- Core pyATS Helper Functions ---

def _get_device(device_name: str):
    """Helper to load testbed and get/connect to a device."""
    try:
        testbed = loader.load(TESTBED_PATH)
        device = testbed.devices.get(device_name)
        if not device:
            raise ValueError(f"Device '{device_name}' not found in testbed '{TESTBED_PATH}'.")

        if not device.is_connected():
            logger.info(f"Connecting to {device_name}...")
            device.connect(
                connection_timeout=120,
                learn_hostname=True,
                log_stdout=False,
                mit=True
            )
            logger.info(f"Connected to {device_name}")

        return device

    except Exception as e:
        logger.error(f"Error getting/connecting to device {device_name}: {e}", exc_info=True)
        raise

def _disconnect_device(device):
    """Helper to safely disconnect."""
    if device and device.is_connected():
        logger.info(f"Disconnecting from {device.name}...")
        try:
            device.disconnect()
            logger.info(f"Disconnected from {device.name}")
        except Exception as e:
            logger.warning(f"Error disconnecting from {device.name}: {e}")

def clean_output(output: str) -> str:
    """Clean ANSI escape sequences and non-printable characters."""
    # Remove ANSI escape sequences
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    output = ansi_escape.sub('', output)
    
    # Remove non-printable control characters
    output = ''.join(char for char in output if char in string.printable)
    
    return output

# --- Core pyATS Functions ---

async def run_show_command_async(device_name: str, command: str) -> Dict[str, Any]:
    """Execute a show command on a device."""
    device = None
    try:
        # Validate command
        disallowed_modifiers = ['|', 'include', 'exclude', 'begin', 'redirect', '>', '<', 'config', 'copy', 'delete', 'erase', 'reload', 'write']
        command_lower = command.lower().strip()
        
        if not command_lower.startswith("show"):
            return {"status": "error", "error": f"Command '{command}' is not a 'show' command."}
        
        for part in command_lower.split():
            if part in disallowed_modifiers:
                return {"status": "error", "error": f"Command '{command}' contains disallowed term '{part}'."}

        # Execute in thread to avoid blocking
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, partial(_execute_show_command, device_name, command))
        return result

    except Exception as e:
        logger.error(f"Error in run_show_command_async: {e}", exc_info=True)
        return {"status": "error", "error": f"Execution error: {e}"}

def _execute_show_command(device_name: str, command: str) -> Dict[str, Any]:
    """Synchronous helper for show command execution."""
    device = None
    try:
        device = _get_device(device_name)
        
        try:
            logger.info(f"Attempting to parse command: '{command}' on {device_name}")
            parsed_output = device.parse(command)
            logger.info(f"Successfully parsed output for '{command}' on {device_name}")
            return {"status": "completed", "device": device_name, "output": parsed_output}
        except Exception as parse_exc:
            logger.warning(f"Parsing failed for '{command}' on {device_name}: {parse_exc}. Falling back to execute.")
            raw_output = device.execute(command)
            logger.info(f"Executed command (fallback): '{command}' on {device_name}")
            return {"status": "completed_raw", "device": device_name, "output": raw_output}
            
    except Exception as e:
        logger.error(f"Error executing show command: {e}", exc_info=True)
        return {"status": "error", "error": f"Execution error: {e}"}
    finally:
        _disconnect_device(device)

async def apply_device_configuration_async(device_name: str, config_commands: str) -> Dict[str, Any]:
    """Apply configuration to a device."""
    try:
        # Safety check
        if "erase" in config_commands.lower() or "write erase" in config_commands.lower():
            logger.warning(f"Rejected potentially dangerous command on {device_name}: {config_commands}")
            return {"status": "error", "error": "Potentially dangerous command detected (erase). Operation aborted."}

        # Execute in thread to avoid blocking
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, partial(_execute_config, device_name, config_commands))
        return result

    except Exception as e:
        logger.error(f"Error in apply_device_configuration_async: {e}", exc_info=True)
        return {"status": "error", "error": f"Configuration error: {e}"}

def _execute_config(device_name: str, config_commands: str) -> Dict[str, Any]:
    """Synchronous helper for configuration application."""
    device = None
    try:
        device = _get_device(device_name)
        
        cleaned_config = textwrap.dedent(config_commands.strip())
        if not cleaned_config:
            return {"status": "error", "error": "Empty configuration provided."}

        logger.info(f"Applying configuration on {device_name}:\n{cleaned_config}")
        output = device.configure(cleaned_config)
        logger.info(f"Configuration result on {device_name}: {output}")
        return {"status": "success", "message": f"Configuration applied on {device_name}.", "output": output}

    except Exception as e:
        logger.error(f"Error applying configuration: {e}", exc_info=True)
        return {"status": "error", "error": f"Configuration error: {e}"}
    finally:
        _disconnect_device(device)

async def execute_learn_config_async(device_name: str) -> Dict[str, Any]:
    """Learn device configuration."""
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, partial(_execute_learn_config, device_name))
        return result
    except Exception as e:
        logger.error(f"Error in execute_learn_config_async: {e}", exc_info=True)
        return {"status": "error", "error": f"Error learning config: {e}"}

def _execute_learn_config(device_name: str) -> Dict[str, Any]:
    """Synchronous helper for learning configuration."""
    device = None
    try:
        device = _get_device(device_name)
        logger.info(f"Learning configuration from {device_name}...")
        
        device.enable()
        raw_output = device.execute("show run brief")
        cleaned_output = clean_output(raw_output)
        
        logger.info(f"Successfully learned config from {device_name}")
        return {
            "status": "completed_raw",
            "device": device_name,
            "output": {"raw_output": cleaned_output}
        }
    except Exception as e:
        logger.error(f"Error learning config: {e}", exc_info=True)
        return {"status": "error", "error": f"Error learning config: {e}"}
    finally:
        _disconnect_device(device)

async def execute_learn_logging_async(device_name: str) -> Dict[str, Any]:
    """Learn device logging."""
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, partial(_execute_learn_logging, device_name))
        return result
    except Exception as e:
        logger.error(f"Error in execute_learn_logging_async: {e}", exc_info=True)
        return {"status": "error", "error": f"Error learning logs: {e}"}

def _execute_learn_logging(device_name: str) -> Dict[str, Any]:
    """Synchronous helper for learning logging."""
    device = None
    try:
        device = _get_device(device_name)
        logger.info(f"Learning logging output from {device_name}...")
        
        raw_output = device.execute("show logging last 250")
        logger.info(f"Successfully learned logs from {device_name}")
        
        return {
            "status": "completed_raw",
            "device": device_name,
            "output": {"raw_output": raw_output}
        }
    except Exception as e:
        logger.error(f"Error learning logs: {e}", exc_info=True)
        return {"status": "error", "error": f"Error learning logs: {e}"}
    finally:
        _disconnect_device(device)

async def learn_feature_async(device_name: str, feature: str, filter_query: Optional[str] = None) -> Dict[str, Any]:
    """Learn a specific networking feature using Genie Ops object."""
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, partial(_execute_learn_feature, device_name, feature, filter_query))
        return result
    except Exception as e:
        logger.error(f"Error in learn_feature_async: {e}", exc_info=True)
        return {"status": "error", "error": f"Error learning feature '{feature}': {e}"}

def _execute_learn_feature(device_name: str, feature: str, filter_query: Optional[str] = None) -> Dict[str, Any]:
    """Synchronous helper to execute device.learn()"""
    device = None
    try:
        device = _get_device(device_name)
        logger.info(f"Learning feature '{feature}' from {device_name} using Genie...")
        
        # device.learn() returns a Genie Ops object containing the deep structured data
        learned_obj = device.learn(feature)
        
        # The structured data is usually stored in the 'info' attribute of the Ops object
        if hasattr(learned_obj, 'info'):
            output_data = learned_obj.info
        if filter_query:
            logger.info(f"Applying Dq query: '{filter_query}'")
            # The query method returns a new Ops object, which we convert to a dictionary
            # This significantly reduces the data sent to the LLM.
            filtered_obj = learned_obj.query(filter_query)
            output_data = filtered_obj.to_dict()
        else:
            if hasattr(learned_obj, 'info'):
                output_data = learned_obj.info
            else:
                output_data = str(learned_obj)
            
        logger.info(f"Successfully learned feature '{feature}' from {device_name}")
        return {
            "status": "completed",
            "device": device_name,
            "feature": feature,
            "filter_applied": filter_query,
            "output": output_data
        }
    except Exception as e:
        logger.error(f"Error learning feature '{feature}': {e}", exc_info=True)
        return {"status": "error", "error": f"Error learning feature '{feature}': {e}"}
    finally:
        _disconnect_device(device)

async def run_ping_command_async(device_name: str, command: str) -> Dict[str, Any]:
    """Execute a ping command on a device."""
    try:
        if not command.lower().strip().startswith("ping"):
            return {"status": "error", "error": f"Command '{command}' is not a 'ping' command."}
        
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, partial(_execute_ping, device_name, command))
        return result
    except Exception as e:
        logger.error(f"Error in run_ping_command_async: {e}", exc_info=True)
        return {"status": "error", "error": f"Ping execution error: {e}"}

def _execute_ping(device_name: str, command: str) -> Dict[str, Any]:
    """Synchronous helper for ping execution."""
    device = None
    try:
        device = _get_device(device_name)
        logger.info(f"Executing ping: '{command}' on {device_name}")
        
        try:
            parsed_output = device.parse(command)
            logger.info(f"Parsed ping output for '{command}' on {device_name}")
            return {"status": "completed", "device": device_name, "output": parsed_output}
        except Exception as parse_exc:
            logger.warning(f"Parsing ping failed for '{command}' on {device_name}: {parse_exc}. Falling back to execute.")
            raw_output = device.execute(command)
            logger.info(f"Executed ping (fallback): '{command}' on {device_name}")
            return {"status": "completed_raw", "device": device_name, "output": raw_output}
    except Exception as e:
        logger.error(f"Error executing ping: {e}", exc_info=True)
        return {"status": "error", "error": f"Ping execution error: {e}"}
    finally:
        _disconnect_device(device)

async def run_linux_command_async(device_name: str, command: str) -> Dict[str, Any]:
    """Execute a Linux command on a device."""
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, partial(_execute_linux_command, device_name, command))
        return result
    except Exception as e:
        logger.error(f"Error in run_linux_command_async: {e}", exc_info=True)
        return {"status": "error", "error": f"Linux command execution error: {e}"}

def _execute_linux_command(device_name: str, command: str) -> Dict[str, Any]:
    """Synchronous helper for Linux command execution."""
    device = None
    try:
        logger.info("Loading testbed...")
        testbed = loader.load(TESTBED_PATH)
        
        if device_name not in testbed.devices:
            return {"status": "error", "error": f"Device '{device_name}' not found in testbed."}
        
        device = testbed.devices[device_name]
        
        if not device.is_connected():
            logger.info(f"Connecting to {device_name} via SSH...")
            device.connect()
        
        if ">" in command or "|" in command:
            logger.info(f"Detected redirection or pipe in command: {command}")
            command = f'sh -c "{command}"'
        
        try:
            parser = get_parser(command, device)
            if parser:
                logger.info(f"Parsing output for command: {command}")
                output = device.parse(command)
            else:
                raise ValueError("No parser available")
        except Exception as e:
            logger.warning(f"No parser found for command: {command}. Using `execute` instead. Error: {e}")
            output = device.execute(command)
        
        logger.info(f"Disconnecting from {device_name}...")
        device.disconnect()
        
        return {"status": "completed", "device": device_name, "output": output}
    except Exception as e:
        logger.error(f"Error executing Linux command: {e}", exc_info=True)
        return {"status": "error", "error": str(e)}
    finally:
        if device and device.is_connected():
            try:
                device.disconnect()
            except:
                pass

async def run_adhoc_command_async(ip: str, os_type: str, username: str, password: str, command: str) -> Dict[str, Any]:
    """Execute a command on a dynamically provided device."""
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, partial(_execute_adhoc_command, ip, os_type, username, password, command))
        return result
    except Exception as e:
        logger.error(f"Error in run_adhoc_command_async: {e}", exc_info=True)
        return {"status": "error", "error": f"Ad-hoc execution error: {e}"}

def _execute_adhoc_command(ip: str, os_type: str, username: str, password: str, command: str) -> Dict[str, Any]:
    """Synchronous helper for Ad-hoc execution."""
    device = None
    try:
        logger.info(f"Creating in-memory testbed for ad-hoc connection to {ip}...")
        testbed = Testbed('dynamic_testbed')
        device = Device('dynamic_device', testbed=testbed, os=os_type, type='router')
        device.connections = {
            "cli": {
                "protocol": "ssh",
                "ip": ip,
                "port": 22,
                "arguments": {"connection_timeout": 60}
            }
        }
        device.credentials = {"default": {"username": username, "password": password}}
        
        device.connect(learn_hostname=True, log_stdout=False, mit=True)
        logger.info(f"Executing ad-hoc command: '{command}' on {ip}")
        output = device.execute(command)
        
        return {"status": "success", "device_ip": ip, "output": output}
    except Exception as e:
        logger.error(f"Error executing ad-hoc command: {e}", exc_info=True)
        return {"status": "error", "error": str(e)}
    finally:
        _disconnect_device(device)

async def add_device_to_testbed_async(device_name: str, ip: str, os_type: str, username: str, password: str, protocol: str) -> Dict[str, Any]:
    """Add a new device to the existing testbed.yaml."""
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, partial(_add_device_to_testbed, device_name, ip, os_type, username, password, protocol))
        return result
    except Exception as e:
        logger.error(f"Error in add_device_to_testbed_async: {e}", exc_info=True)
        return {"status": "error", "error": f"Testbed update error: {e}"}

def _add_device_to_testbed(device_name: str, ip: str, os_type: str, username: str, password: str, protocol: str) -> Dict[str, Any]:
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
                "cli": {"protocol": protocol, "ip": ip, "port": 22, "arguments": {"connection_timeout": 60}}
            },
            "credentials": {"default": {"username": username, "password": password}}
        }

        with open(TESTBED_PATH, 'w') as f:
            yaml.dump(tb_data, f, default_flow_style=False, sort_keys=False)

        return {"status": "success", "message": f"Device '{device_name}' successfully added to testbed."}
    except Exception as e:
        logger.error(f"Error updating testbed.yaml: {e}", exc_info=True)
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
async def pyats_run_show_command(device_name: str, command: str) -> str:
    """
    Execute a Cisco IOS/NX-OS 'show' command on a specified device.
    
    Args:
        device_name: The name of the device in the testbed
        command: The show command to execute (e.g., 'show ip interface brief')
    
    Returns:
        JSON string containing the command output
    """
    try:
        result = await run_show_command_async(device_name, command)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error in pyats_run_show_command: {e}", exc_info=True)
        return json.dumps({"status": "error", "error": str(e)}, indent=2)

@mcp.tool()
async def pyats_configure_device(device_name: str, config_commands: str) -> str:
    """
    Apply configuration commands to a Cisco IOS/NX-OS device.
    
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
async def pyats_learn_feature(device_name: str, feature: str, filter_query: Optional[str] = None) -> str:
    """
    Learn a specific network feature (e.g., 'ospf', 'isis', 'bgp', 'routing', 'interface') 
    using Cisco Genie Ops objects. This retrieves a deep, OS-agnostic structured JSON database.
    Optionally, a Dq query can be used to filter the output on the server side.
    
    Args:
        device_name: The name of the device in the testbed
        feature: The feature to learn (e.g., 'ospf', 'isis')
        filter_query: (Optional) A Genie Dq query string to filter the output. E.g., for 'ospf', use 'info.vrf["default"].address_family["ipv4"].instance["1"].areas["0.0.0.0"].interfaces' to get only interface data.
    
    Returns:
        JSON string containing the deeply parsed operational state of the feature.
    """
    try:
        result = await learn_feature_async(device_name, feature, filter_query)
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
async def add_device_to_testbed(device_name: str, ip: str, os_type: str, username: str, password: str, protocol: str = "ssh") -> str:
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
    """
    try:
        result = await add_device_to_testbed_async(device_name, ip, os_type, username, password, protocol)
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

# --- Main Function ---
if __name__ == "__main__":
    logger.info("🚀 Starting pyATS FastMCP Server...")
    mcp.run()