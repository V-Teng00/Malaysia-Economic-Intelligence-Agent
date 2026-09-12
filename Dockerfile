# Pin the base image by digest-free tag but to a specific minor version —
# avoid ":latest", it silently changes what you ship between builds.
FROM python:3.12-slim

# --- Security basics ---
# 1. Non-root user: if the app is ever compromised via a dependency or a
#    parsing bug, it doesn't get root inside the container.
RUN groupadd --gid 1000 appgroup && \
    useradd --uid 1000 --gid appgroup --create-home --shell /bin/bash appuser

WORKDIR /app

# 2. Install deps as root (needed for system-level installs), then drop
#    privileges before running anything that touches network input.
COPY server/requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY server/server.py .

# 3. Never bake secrets into the image. This server currently needs none
#    (data.gov.my requires no auth). When the agent layer is added and needs
#    ANTHROPIC_API_KEY, pass it at `docker run -e` / a Docker secret / an
#    orchestrator-managed secret store — NEVER an ENV line in this file and
#    NEVER committed into requirements.txt, server.py, or .env checked into git.

USER appuser

EXPOSE 8000

# 4. Basic healthcheck so an orchestrator (Compose, ECS) can detect a hung
#    process rather than routing traffic to a dead server.
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/', timeout=3)" || exit 1

CMD ["python", "server.py"]
