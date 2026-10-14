FROM python:3.12-slim

RUN useradd --create-home --uid 10002 mcpauth

WORKDIR /app
COPY pyproject.toml README.md ./
COPY mcp_auth ./mcp_auth

RUN pip install --no-cache-dir . \
    && mkdir -p /data && chown -R mcpauth:mcpauth /data

USER mcpauth
ENV PYTHONUNBUFFERED=1

VOLUME ["/data"]

# One worker, several threads — deliberately.
#
# Pending authorization requests live in process memory for the length
# of one browser round-trip. With multiple worker processes, the consent
# POST could land on a worker that never saw the authorize GET, and the
# login would fail intermittently in a way that is miserable to debug.
# Threads share that memory; processes do not.
CMD ["gunicorn", "--workers", "1", "--threads", "8", \
     "--bind", "0.0.0.0:8780", \
     "--access-logfile", "-", "--error-logfile", "-", \
     "mcp_auth.app:create_app()"]
