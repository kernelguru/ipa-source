# ---------------------------------------------------------------- zsign --
FROM python:3.13-slim AS zsign
ARG ZSIGN_VERSION=v1.1.3
RUN apt-get update \
 && apt-get install -y --no-install-recommends g++ make pkg-config libssl-dev git ca-certificates \
 && git clone --depth 1 --branch "$ZSIGN_VERSION" https://github.com/zhlynn/zsign.git /src/zsign \
 && make -C /src/zsign/build/linux -j"$(nproc)" VERSION="${ZSIGN_VERSION#v}" \
 && /src/zsign/bin/zsign -v

# ------------------------------------------------------------------ app --
FROM python:3.13-slim

# libimobiledevice: USB installation to an iPhone attached to the host
RUN apt-get update \
 && apt-get install -y --no-install-recommends libimobiledevice-utils ideviceinstaller usbmuxd \
 && rm -rf /var/lib/apt/lists/*
COPY --from=zsign /src/zsign/bin/zsign /usr/local/bin/zsign

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY docs ./docs
COPY fixes/bin ./fixes/bin
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENV DATA_DIR=/data \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
