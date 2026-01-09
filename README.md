# HidrateSpark-BLE-Reader

A Docker container that uses the host's Bluetooth connection to read data from a HidrateSpark PRO bottle via BLE (Bluetooth Low Energy).

## Features

- 🔍 **Scan for HidrateSpark bottles** - Automatically discover nearby bottles
- 🔗 **Simple connection management** - Easy web-based interface to connect/disconnect
- 📊 **Real-time data monitoring** - View hydration data including sip size, total intake, and timestamps
- ⏰ **Automatic data sync** - Periodically downloads data from the bottle every 10 minutes
- 🌐 **Web interface** - User-friendly interface accessible from any browser
- 🐳 **Docker ready** - Runs in a container with access to host Bluetooth
- 📦 **GHCR hosted** - Automatically built and published to GitHub Container Registry

## Requirements

- Docker
- Bluetooth adapter on the host
- HidrateSpark PRO bottle

## Quick Start

### Using Docker Compose (Recommended)

Create a `docker-compose.yml` file:

```yaml
version: '3.8'

services:
  hidrate-reader:
    image: ghcr.io/loryanstrant/hidratespark-ble-reader:latest
    container_name: hidrate-reader
    network_mode: host
    volumes:
      - /var/run/dbus:/var/run/dbus
    cap_add:
      - NET_ADMIN
    devices:
      - /dev/bus/usb:/dev/bus/usb
    restart: unless-stopped
```

Then run:

```bash
docker-compose up -d
```

### Using Docker Run

```bash
docker run -d \
  --name hidrate-reader \
  --network host \
  --cap-add NET_ADMIN \
  -v /var/run/dbus:/var/run/dbus \
  --device /dev/bus/usb:/dev/bus/usb \
  --restart unless-stopped \
  ghcr.io/loryanstrant/hidratespark-ble-reader:latest
```

### Access the Web Interface

Open your browser and navigate to:

```
http://localhost:5000
```

## Usage

1. **Scan for Devices**: Click the "Scan for Bottles" button to discover nearby HidrateSpark bottles
2. **Connect**: Click "Connect" next to your bottle in the device list
3. **Monitor**: View real-time battery level, bottle information, and hydration data
4. **Auto-sync**: The container will automatically request data updates every 10 minutes
5. **Manual Update**: Click "Request Update" to manually fetch the latest data

## Building from Source

Clone the repository and build the Docker image:

```bash
git clone https://github.com/loryanstrant/HidrateSpark-BLE-Reader.git
cd HidrateSpark-BLE-Reader
docker build -t hidrate-reader .
```

## Development

### Local Development without Docker

1. Install Python dependencies:
```bash
pip install -r requirements.txt
```

2. Run the application:
```bash
python app.py
```

3. Access at `http://localhost:5000`

## Architecture

The application consists of three main components:

- **hidrate_ble.py**: BLE interface module for communicating with HidrateSpark bottles
- **app.py**: Flask web application providing REST API and serving the web interface
- **templates/index.html**: Responsive web UI for device management and data visualization

## Bluetooth Permissions

The container requires:
- `--network host`: Access to host network for BLE communication
- `--cap-add NET_ADMIN`: Network administration capabilities
- `-v /var/run/dbus:/var/run/dbus`: DBus socket for Bluetooth communication
- `--device /dev/bus/usb:/dev/bus/usb`: USB device access (if using USB Bluetooth adapter)

## Troubleshooting

### Container can't find Bluetooth adapter

Ensure your host has a working Bluetooth adapter:
```bash
bluetoothctl list
```

### Can't scan for devices

Make sure Bluetooth is enabled on the host:
```bash
sudo systemctl status bluetooth
```

### Permission denied errors

Ensure the container has the necessary capabilities and device access as shown in the docker run command.

## Credits

This project builds upon code and insights from:
- [TheCrushinator.Home.Health.BottleSync](https://github.com/The-Crushinator/TheCrushinator.Home.Health.BottleSync)
- [wban-python](https://github.com/choonkiatlee/wban-python)

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
