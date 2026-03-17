FROM python:3.12-slim

# Install age (for decryption) and curl
RUN apt-get update -y && \
    apt-get install -y --no-install-recommends age curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

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
