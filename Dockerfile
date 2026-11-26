FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .
# HTTP mode has no authentication of its own: put an authenticating proxy or MCP gateway in front of it.
ENV MCP_TRANSPORT=http HOST=0.0.0.0 PORT=8000
EXPOSE 8000
USER 1000
CMD ["fortnox-mcp"]
