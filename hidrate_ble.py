"""
HidrateSpark BLE interface module.
Based on code from:
- https://github.com/The-Crushinator/TheCrushinator.Home.Health.BottleSync
- https://github.com/choonkiatlee/wban-python
"""
import asyncio
from datetime import datetime
from bleak import BleakScanner, BleakClient


class HidrateSpark:
    """Interface for HidrateSpark PRO bottle via BLE."""
    
    # BLE Characteristic UUIDs
    BATTERY = '00002a19-0000-1000-8000-00805f9b34fb'
    MODEL_NUMBER = '00002a24-0000-1000-8000-00805f9b34fb'
    MANUFACTURER_NAME = '00002a29-0000-1000-8000-00805f9b34fb'
    SERIAL_NUMBER = '00002a25-0000-1000-8000-00805f9b34fb'
    FIRMWARE_VERSION = '00002a26-0000-1000-8000-00805f9b34fb'
    HARDWARE_VERSION = '00002a27-0000-1000-8000-00805f9b34fb'
    SOFTWARE_VERSION = '00002a28-0000-1000-8000-00805f9b34fb'
    
    # Bottle-specific characteristics
    DATA_POINT = '016e11b1-6c8a-4074-9e5a-076053f93784'
    DEBUG = 'e3578b0d-caa7-46d6-b7c2-7331c08de044'
    LED_CONTROL = 'a1d9a5bf-f5d8-49f3-a440-e6bf27440cb0'
    
    # Protocol constants
    APPEARANCE_CHARACTERISTIC_HANDLE = 12  # Handle for bottle size
    REQUEST_SIP_DATA_CMD = 0x57  # Command to request sip data
    
    def __init__(self):
        self.bottle_size = 0
        self.sip_data = []
        self.connected = False
        self.client = None
        
    @staticmethod
    async def scan_devices(timeout=5):
        """Scan for BLE devices and return HidrateSpark bottles."""
        devices = await BleakScanner.discover(timeout=timeout)
        bottles = []
        for device in devices:
            if device.name and "h2o" in device.name.lower():
                bottles.append({
                    'address': device.address,
                    'name': device.name,
                    'rssi': device.rssi
                })
        return bottles
    
    async def connect(self, address):
        """Connect to a HidrateSpark bottle."""
        self.client = BleakClient(address, timeout=30)
        await self.client.connect()
        self.connected = self.client.is_connected
        
        if self.connected:
            # Get bottle size from appearance characteristic
            appearance = await self.client.read_gatt_char(self.APPEARANCE_CHARACTERISTIC_HANDLE)
            self.bottle_size = int.from_bytes(appearance[0:2], byteorder='little')
            
            # Start notifications for sip data
            await self.client.start_notify(self.DATA_POINT, self._handle_sip_notification)
            
        return self.connected
    
    async def disconnect(self):
        """Disconnect from the bottle."""
        if self.client and self.connected:
            await self.client.stop_notify(self.DATA_POINT)
            await self.client.disconnect()
            self.connected = False
    
    async def get_device_info(self):
        """Get device information."""
        if not self.connected:
            return None
            
        info = {
            'battery': int.from_bytes(await self.client.read_gatt_char(self.BATTERY), byteorder='little'),
            'model': (await self.client.read_gatt_char(self.MODEL_NUMBER)).decode('utf-8'),
            'manufacturer': (await self.client.read_gatt_char(self.MANUFACTURER_NAME)).decode('utf-8'),
            'serial': (await self.client.read_gatt_char(self.SERIAL_NUMBER)).decode('utf-8'),
            'firmware': (await self.client.read_gatt_char(self.FIRMWARE_VERSION)).decode('utf-8'),
            'hardware': (await self.client.read_gatt_char(self.HARDWARE_VERSION)).decode('utf-8'),
            'software': (await self.client.read_gatt_char(self.SOFTWARE_VERSION)).decode('utf-8'),
            'bottle_size': self.bottle_size
        }
        return info
    
    async def request_sip_data(self):
        """Request sip data from the bottle."""
        if not self.connected:
            return False
        
        # Write command to request next sip
        await self.client.write_gatt_char(self.DATA_POINT, bytearray([self.REQUEST_SIP_DATA_CMD]))
        return True
    
    def _handle_sip_notification(self, sender, data):
        """Handle sip data notifications."""
        if len(data) == 0:
            return
            
        # Check if this is a sip count notification or actual sip data
        if data[0] > 0 and len(data) > 1 and int.from_bytes(data[1:], byteorder='little') > 0:
            # Parse sip data
            sip = self._parse_sip(data)
            if sip:
                self.sip_data.append(sip)
        elif data[0] > 0:
            # Number of sips remaining notification
            print(f"Sips remaining: {data[0]}")
        else:
            print("No sip data available")
    
    def _parse_sip(self, data):
        """Parse sip notification data."""
        if len(data) < 16:
            return None
            
        sips_remaining = data[0]
        sip_percentage = data[1]
        sip_total = int.from_bytes(data[2:4], byteorder='little')
        sip_seconds_ago = int.from_bytes(data[4:8], byteorder='little')
        sip_min = int.from_bytes(data[8:10], byteorder='little')
        sip_max = int.from_bytes(data[10:12], byteorder='little')
        sip_start = int.from_bytes(data[12:14], byteorder='little')
        sip_stop = int.from_bytes(data[14:16], byteorder='little')
        
        # Calculate sip size using sensor data range
        sensor_range = sip_max - min(sip_start, sip_min)
        if sensor_range > 0:
            start_percentage = max(0.0, min(1.0, (sip_start - min(sip_start, sip_min)) / sensor_range))
            stop_percentage = max(0.0, min(1.0, (sip_stop - min(sip_stop, sip_min)) / sensor_range))
            sip_size = (start_percentage - stop_percentage) * self.bottle_size
        else:
            sip_size = 0
        
        return {
            'timestamp': datetime.now().isoformat(),
            'sip_size': abs(sip_size),
            'total': sip_total,
            'seconds_ago': sip_seconds_ago,
            'percentage': sip_percentage
        }
    
    def get_sip_data(self):
        """Get all collected sip data."""
        return self.sip_data
    
    def clear_sip_data(self):
        """Clear collected sip data."""
        self.sip_data = []
