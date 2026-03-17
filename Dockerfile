# ---- Build stage ----
FROM python:3.12-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --target /build/deps -r requirements.txt

# ---- Runtime stage ----
FROM python:3.12-slim

# Install age (for decryption)
RUN apt-get update -y && \
    apt-get install -y --no-install-recommends age curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependencies from build stage
COPY --from=builder /build/deps /usr/local/lib/python3.12/site-packages

# Copy application code
COPY app/ app/
COPY scheduler.py .
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

# Data volume: config, tokens, logs, keys
VOLUME ["/data"]

ENV PORT=8000
ENV CONFIG_FILE=/data/config.json
ENV SCHEDULE_HOURS=1

EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]
