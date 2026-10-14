FROM python:3.12-slim

# Non-root by default; the container only needs outbound 993 and /data.
RUN useradd --create-home --uid 10001 imap

WORKDIR /app

COPY pyproject.toml README.md ./
COPY imap_mcp ./imap_mcp

RUN pip install --no-cache-dir . \
    && mkdir -p /data \
    && chown -R imap:imap /data

USER imap

ENV IMAP_MCP_CONFIG=/config/config.toml \
    PYTHONUNBUFFERED=1

VOLUME ["/data"]

# Cheap liveness probe: the process answers on the HTTP port.
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,socket,sys; \
port=int(os.environ.get('IMAP_MCP_HTTP_PORT','8770')); \
s=socket.create_connection(('127.0.0.1',port),3); s.close()" || exit 1

ENTRYPOINT ["imap-mcp"]
CMD ["serve"]
