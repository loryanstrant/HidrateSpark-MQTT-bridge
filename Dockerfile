FROM python:3.11-slim

# Install system dependencies for Bluetooth
RUN apt-get update && apt-get install -y \
    bluez \
    bluetooth \
    libbluetooth-dev \
    libglib2.0-dev \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY app.py .
COPY hidrate_ble.py .
COPY templates/ templates/

# Expose port for web interface
EXPOSE 5000

# Run the application
CMD ["python", "app.py"]
