FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        bluez \
        dbus \
        libglib2.0-0 \
        ca-certificates \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app /app/app

ENV CONFIG_PATH=/config/config.yaml \
    PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["python", "-m", "app"]
