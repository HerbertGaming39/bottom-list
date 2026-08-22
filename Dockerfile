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

# Runtime user matching host volume ownership (marcel = uid/gid 1000)
RUN groupadd -r -g 1000 appgroup && useradd -r -u 1000 -g 1000 -d /app appuser \
    && mkdir -p /app/instance /app/logs /app/backups \
    && chown -R appuser:appgroup /app

USER appuser

EXPOSE 6890

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:6890/robots.txt', timeout=4)" || exit 1

CMD ["python", "app.py"]
