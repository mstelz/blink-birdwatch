FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

RUN chmod +x /app/bin/blink_cli.py /app/bin/blink_auth.py /app/bin/blink_fetch.py /app/bin/blink_service.py \
    && ln -sf /app/bin/blink_cli.py /usr/local/bin/blink \
    && mkdir -p /app/config /app/work /app/output

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:${PORT:-8787}/health || exit 1

CMD ["/app/bin/start_birdwatch.sh"]
