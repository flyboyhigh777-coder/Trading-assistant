FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, so edits to the source do not re-resolve the wheel set.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pockettrader ./pockettrader

# Subscribers and invoices live here; mount a volume so they survive a redeploy.
RUN mkdir -p /app/data && useradd --create-home --uid 10001 pockettrader \
    && chown -R pockettrader:pockettrader /app
USER pockettrader

ENV POCKETTRADER_SUBSCRIBERS_PATH=/app/data/subscribers.json \
    POCKETTRADER_WEBHOOK_HOST=0.0.0.0 \
    POCKETTRADER_WEBHOOK_PORT=8080

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4).status == 200 else 1)"

ENTRYPOINT ["python", "-m", "pockettrader"]
CMD ["bot"]
