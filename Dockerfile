# Dockerfile for trae-builder MCP server (used by Glama verification).
# The server is a pure-Python stdio MCP server (zero third-party deps for
# build/flash; pyserial only for serial capture, which is not exercised during
# Glama's introspection check). We install pyserial anyway for completeness.
FROM python:3.11-slim

WORKDIR /app

# Copy plugin files.
COPY trae_build_runner.py trae_build_mcp.py trae_build_init.py trae_builder_schema.json ./
COPY scripts/ ./scripts/
COPY commands/ ./commands/
COPY skills/ ./skills/
COPY traecli.yaml ./

# pyserial is optional but include it so serial tools are functional.
RUN pip install --no-cache-dir pyserial

# The MCP server speaks stdio. Glama launches it and sends JSON-RPC
# (initialize / tools/list). No port exposure needed for stdio servers.
ENTRYPOINT ["python", "trae_build_mcp.py"]
