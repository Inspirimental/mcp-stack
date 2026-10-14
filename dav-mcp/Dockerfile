FROM python:3.12-slim

RUN useradd --create-home --uid 10003 dav

WORKDIR /app
COPY pyproject.toml README.md ./
COPY dav_mcp ./dav_mcp

RUN pip install --no-cache-dir .

USER dav

ENV DAV_MCP_CONFIG=/config/config.toml \
    PYTHONUNBUFFERED=1

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,socket; \
port=int(os.environ.get('DAV_MCP_HTTP_PORT','8771')); \
s=socket.create_connection(('127.0.0.1',port),3); s.close()" || exit 1

ENTRYPOINT ["dav-mcp"]
CMD ["serve"]
