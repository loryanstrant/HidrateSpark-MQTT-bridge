"""FastAPI web UI for configuration and status."""
from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

from bleak import BleakScanner
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from .config import BottleConfig, MqttConfig, Settings, load_settings, save_settings

log = logging.getLogger(__name__)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def create_app(orchestrator) -> FastAPI:
    app = FastAPI(title="HidrateSpark MQTT Bridge")

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        return TEMPLATES.TemplateResponse(
            request,
            "dashboard.html",
            {
                "settings": orchestrator.settings,
                "status": orchestrator.state.snapshot(),
                "mqtt_connected": orchestrator.mqtt.connected,
            },
        )

    @app.get("/api/status")
    async def api_status():
        snap = orchestrator.state.snapshot()
        snap["mqtt_connected"] = orchestrator.mqtt.connected
        snap["recent_sips"] = orchestrator.state.recent_sips(20)
        return JSONResponse(snap)

    @app.post("/api/reconnect")
    async def api_reconnect():
        orchestrator.ble.request_reconnect()
        return {"ok": True}

    @app.post("/api/force_sync")
    async def api_force_sync():
        orchestrator.ble.request_force_sync()
        return {"ok": True}

    @app.post("/api/scan")
    async def api_scan():
        try:
            devs = await BleakScanner.discover(timeout=12.0)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        result = []
        for d in devs:
            name = d.name or ""
            if name.lower().startswith("h2o") or name.lower().startswith("hidrate"):
                result.insert(0, {"address": d.address, "name": name, "match": True})
            else:
                result.append({"address": d.address, "name": name or "(unknown)", "match": False})
        return {"devices": result}

    @app.get("/settings/mqtt", response_class=HTMLResponse)
    async def settings_mqtt_get(request: Request):
        return TEMPLATES.TemplateResponse(
            request,
            "settings_mqtt.html",
            {"mqtt": orchestrator.settings.mqtt},
        )

    @app.post("/settings/mqtt")
    async def settings_mqtt_post(
        enabled: Optional[str] = Form(None),
        host: str = Form(""),
        port: int = Form(1883),
        username: str = Form(""),
        password: str = Form(""),
        tls: Optional[str] = Form(None),
        base_topic: str = Form("hidrate"),
        client_id: str = Form("hidrate-mqtt-bridge"),
        ha_discovery: Optional[str] = Form(None),
        ha_discovery_prefix: str = Form("homeassistant"),
    ):
        new = MqttConfig(
            enabled=bool(enabled),
            host=host.strip(),
            port=int(port),
            username=username.strip(),
            password=password,
            tls=bool(tls),
            base_topic=base_topic.strip() or "hidrate",
            client_id=client_id.strip() or "hidrate-mqtt-bridge",
            ha_discovery=bool(ha_discovery),
            ha_discovery_prefix=ha_discovery_prefix.strip() or "homeassistant",
        )
        orchestrator.settings.mqtt = new
        save_settings(orchestrator.settings)
        orchestrator.mqtt.update_config(new, orchestrator.settings.bottle.mac)
        return RedirectResponse("/settings/mqtt?saved=1", status_code=303)

    @app.get("/settings/bottle", response_class=HTMLResponse)
    async def settings_bottle_get(request: Request):
        return TEMPLATES.TemplateResponse(
            request,
            "settings_bottle.html",
            {"bottle": orchestrator.settings.bottle},
        )

    @app.post("/settings/bottle")
    async def settings_bottle_post(
        mac: str = Form(""),
        name_prefix: str = Form("h2o"),
        size_ml: int = Form(591),
        poll_interval_s: int = Form(30),
    ):
        new = BottleConfig(
            mac=mac.strip().upper(),
            name_prefix=name_prefix.strip().lower() or "h2o",
            size_ml=int(size_ml),
            poll_interval_s=int(poll_interval_s),
        )
        orchestrator.settings.bottle = new
        save_settings(orchestrator.settings)
        orchestrator.ble.update_target(new.mac, new.name_prefix, new.size_ml)
        orchestrator.state.set_bottle_size(new.size_ml)
        # Update MQTT device identifiers if MAC changed
        orchestrator.mqtt.update_config(orchestrator.settings.mqtt, new.mac)
        return RedirectResponse("/settings/bottle?saved=1", status_code=303)

    return app
