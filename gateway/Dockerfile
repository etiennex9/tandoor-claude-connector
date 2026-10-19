# Node 24 for tandoor-recipes-mcp, plus a Python venv for the FastMCP gateway.
FROM node:24-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @cliftonz/tandoor-recipes-mcp@2.0.1 && npm cache clean --force

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FASTMCP_HOME=/data

WORKDIR /app
COPY requirements.txt .
RUN python3 -m venv /opt/venv && pip install --no-cache-dir -r requirements.txt

COPY server.py photo_import.py entrypoint.sh ./
RUN chmod 755 entrypoint.sh && mkdir -p /data && chown node:node /data
USER node

EXPOSE 8000
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["uvicorn", "server:build_app", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
