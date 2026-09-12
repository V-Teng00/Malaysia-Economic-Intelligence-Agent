FROM python:3.12-slim

RUN groupadd --gid 1000 appgroup && \
    useradd --uid 1000 --gid appgroup --create-home --shell /bin/bash appuser

WORKDIR /app

COPY server/requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY server/server.py .

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/', timeout=3)" || exit 1

CMD ["python", "server.py"]
