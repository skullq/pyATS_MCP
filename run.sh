docker run -i --rm \
  -e PYATS_TESTBED_PATH=/app/testbed.yaml \
  -v /root/pyATS_MCP:/app \
  pyats-mcp-server
