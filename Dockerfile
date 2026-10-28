# Runs the stdio MCP server (used by directories such as Glama to start the server and list its tools).
# The server makes no network call until a tool is used; the paid tool needs X402_BUYER_PRIVATE_KEY.
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .
ENTRYPOINT ["signal-feed-mcp"]
