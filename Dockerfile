FROM node:22-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    python3 \
    python3-pip \
    ca-certificates \
    curl \
    && python3 -m pip install --no-cache-dir --break-system-packages blinkpy \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY package*.json ./
RUN npm ci --omit=dev

COPY . .

RUN chmod +x /app/bin/blink_cli.py \
    && ln -sf /app/bin/blink_cli.py /usr/local/bin/blink \
    && mkdir -p /app/config /app/work /app/output

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:${PORT:-8787}/health || exit 1

CMD ["node", "src/index.js"]
