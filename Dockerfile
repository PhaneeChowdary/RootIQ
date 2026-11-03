# RootIQ backend: the API and the agent workers use this image (docker-compose.yml).
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

COPY backend/requirements.txt backend/requirements.txt
RUN pip install -r backend/requirements.txt

COPY backend backend
RUN useradd --create-home --uid 10001 rootiq && mkdir -p /data && chown rootiq /data
USER rootiq

ENV ROOTIQ_DATA_DIR=/data
EXPOSE 8011
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8011/readyz', timeout=4)"
# --proxy-headers: the sign-in limits see the real address of each person behind the web proxy
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8011", "--proxy-headers", "--forwarded-allow-ips", "*"]
