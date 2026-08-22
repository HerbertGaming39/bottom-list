# Hardened container image for The Bottom List
# - slim base, non-root, no package tools left at runtime beyond pip cache cleared
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Non-root runtime user; data dirs are mounted as volumes from the host
RUN groupadd -r -g 10001 appuser && useradd -r -u 10001 -g appuser appuser \
    && mkdir -p /app/instance /app/logs /app/backups \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 6890

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:6890/robots.txt', timeout=4)" || exit 1

CMD ["python", "app.py"]
