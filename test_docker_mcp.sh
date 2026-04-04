#!/bin/bash
set -e

echo "==> Building Docker Image..."
docker build -t pyats-mcp-server .

echo "==> Testing direct_ssh_configure tool via Docker..."
echo '{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "direct_ssh_configure", "arguments": {"ip": "172.20.20.5", "username": "admin", "password": "admin", "config_commands": "interface Loopback 100\nip address 100.100.100.100 255.255.255.255"}}}' \
  | docker run -i --rm -e PYATS_TESTBED_PATH=/app/testbed.yaml -v $(pwd):/app pyats-mcp-server --oneshot

echo -e "\n==> Test complete."
