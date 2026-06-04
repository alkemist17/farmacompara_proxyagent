# ── Stage 1: build stage (gcc needed for some curl_cffi wheels) ───────────────
FROM python:3.12-slim AS builder
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc python3-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY node_agent/requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ── Stage 2: lean runtime image ───────────────────────────────────────────────
FROM python:3.12-slim
# curl_cffi needs CA certificates at runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY node_agent/ ./node_agent/

RUN groupadd -r appgroup && useradd -r -g appgroup -d /app appuser && \
    chown -R appuser:appgroup /app
USER appuser

EXPOSE 8765

# TCP-level health check — /ping requires X-Agent-Key so check the port instead
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c \
    "import socket; s=socket.socket(); s.settimeout(4); s.connect(('localhost',8765)); s.close()" \
      || exit 1

CMD ["uvicorn", "node_agent.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8765", \
     "--log-level", "info"]
