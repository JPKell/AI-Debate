# syntax=docker/dockerfile:1.7
# Production container image for AI-Debate (FastAPI + SQLite WAL + Vanilla ES Modules)
#
# Build:
#   docker build -t ai-debate:latest .
#
# Run (auto-starts on host boot via Docker restart policy):
#   docker run -d \
#     --name ai-debate \
#     --restart unless-stopped \
#     -p 8008:8008 \
#     -v ai-debate-data:/app/data \
#     --env-file .env \
#     ai-debate:latest

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    AI_DEBATE_HOST=0.0.0.0 \
    AI_DEBATE_PORT=8008 \
    AI_DEBATE_DATA_DIR=/app/data

WORKDIR /app

# Install runtime dependencies
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy only production application source
COPY app /app/app
COPY run.py /app/run.py

# Create dedicated non-root user and persistent data volume directory
RUN groupadd --gid 1000 aidebate \
    && useradd --uid 1000 --gid 1000 -m -s /usr/sbin/nologin aidebate \
    && mkdir -p /app/data \
    && chown -R aidebate:aidebate /app

USER aidebate

VOLUME ["/app/data"]
EXPOSE 8008

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8008/api/health', timeout=3)"]

CMD ["python", "run.py"]
