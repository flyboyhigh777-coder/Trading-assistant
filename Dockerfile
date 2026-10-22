FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Сначала зависимости: правка исходников не должна пересобирать колёса.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY uzinvest ./uzinvest

# Каталог выпусков монтируется томом — его правят без пересборки образа.
RUN mkdir -p /app/data && useradd --create-home --uid 10001 uzinvest \
    && chown -R uzinvest:uzinvest /app
USER uzinvest

ENV UZINVEST_SUBSCRIBERS_PATH=/app/data/subscribers.json \
    UZINVEST_CATALOG_PATH=/app/data/bonds.yaml \
    UZINVEST_WEBHOOK_HOST=0.0.0.0 \
    UZINVEST_WEBHOOK_PORT=8080

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4).status == 200 else 1)"

ENTRYPOINT ["python", "-m", "uzinvest"]
CMD ["bot"]
