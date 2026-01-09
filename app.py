"""
Flask web application for HidrateSpark BLE Reader.
Provides a web interface to scan, connect, and read data from HidrateSpark bottles.
"""
import asyncio
import json
from datetime import datetime
from flask import Flask, render_template, jsonify, request
from threading import Thread, Lock
from hidrate_ble import HidrateSpark


app = Flask(__name__)
app.config['SECRET_KEY'] = 'hidrate-spark-ble-reader'

# Global state
bottle = HidrateSpark()
bottle_lock = Lock()
selected_device = None
monitoring_task = None
monitoring_active = False


def run_async(coro):
    """Helper to run async code in sync context."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@app.route('/')
def index():
    """Main page."""
    return render_template('index.html')


@app.route('/api/scan', methods=['GET'])
def scan():
    """Scan for HidrateSpark devices."""
    try:
        devices = run_async(HidrateSpark.scan_devices(timeout=10))
        return jsonify({'success': True, 'devices': devices})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/connect', methods=['POST'])
def connect():
    """Connect to a HidrateSpark device."""
    global selected_device, monitoring_active
    
    data = request.json
    address = data.get('address')
    
    if not address:
        return jsonify({'success': False, 'error': 'No address provided'}), 400
    
    try:
        with bottle_lock:
            # Disconnect if already connected
            if bottle.connected:
                run_async(bottle.disconnect())
            
            # Connect to new device
            success = run_async(bottle.connect(address))
            
            if success:
                selected_device = address
                # Start monitoring
                start_monitoring()
                return jsonify({'success': True})
            else:
                return jsonify({'success': False, 'error': 'Failed to connect'}), 500
                
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/disconnect', methods=['POST'])
def disconnect():
    """Disconnect from the current device."""
    global selected_device, monitoring_active
    
    try:
        with bottle_lock:
            if bottle.connected:
                stop_monitoring()
                run_async(bottle.disconnect())
                selected_device = None
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/status', methods=['GET'])
def status():
    """Get current connection status."""
    with bottle_lock:
        if bottle.connected:
            try:
                info = run_async(bottle.get_device_info())
                return jsonify({
                    'connected': True,
                    'address': selected_device,
                    'info': info,
                    'monitoring': monitoring_active
                })
            except Exception as e:
                return jsonify({
                    'connected': False,
                    'error': str(e)
                })
        else:
            return jsonify({'connected': False})


@app.route('/api/data', methods=['GET'])
def get_data():
    """Get collected sip data."""
    with bottle_lock:
        data = bottle.get_sip_data()
        return jsonify({'success': True, 'data': data})


@app.route('/api/request_update', methods=['POST'])
def request_update():
    """Manually request sip data update."""
    try:
        with bottle_lock:
            if not bottle.connected:
                return jsonify({'success': False, 'error': 'Not connected'}), 400
            
            success = run_async(bottle.request_sip_data())
            return jsonify({'success': success})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


def monitoring_loop():
    """Background task to periodically request data."""
    global monitoring_active
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    async def monitor():
        while monitoring_active:
            try:
                with bottle_lock:
                    if bottle.connected:
                        await bottle.request_sip_data()
            except Exception as e:
                print(f"Error in monitoring loop: {e}")
            
            # Wait 10 minutes before next update
            await asyncio.sleep(600)
    
    try:
        loop.run_until_complete(monitor())
    finally:
        loop.close()


def start_monitoring():
    """Start the background monitoring task."""
    global monitoring_task, monitoring_active
    
    if not monitoring_active:
        monitoring_active = True
        monitoring_task = Thread(target=monitoring_loop, daemon=True)
        monitoring_task.start()


def stop_monitoring():
    """Stop the background monitoring task."""
    global monitoring_active
    monitoring_active = False


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
