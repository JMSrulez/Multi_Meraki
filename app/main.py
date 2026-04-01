import os
import asyncio
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

APP_TITLE = "Meraki Personal Dashboard"
DB_PATH = os.getenv("DATABASE_PATH", "/data/meraki.db")
MERAKI_API_KEY = os.getenv("MERAKI_API_KEY", "")
MERAKI_BASE_URL = os.getenv("MERAKI_BASE_URL", "https://api.meraki.com/api/v1")

app = FastAPI(title=APP_TITLE)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(
            """
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS organizations (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                url TEXT,
                api_enabled INTEGER,
                licensing_model TEXT,
                device_count INTEGER NOT NULL DEFAULT 0,
                last_synced_at TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS devices (
                serial TEXT PRIMARY KEY,
                organization_id TEXT NOT NULL,
                name TEXT,
                model TEXT,
                product_type TEXT,
                network_id TEXT,
                mac TEXT,
                lan_ip TEXT,
                firmware TEXT,
                status TEXT,
                tags TEXT,
                details_json TEXT,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS sync_state (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT NOT NULL
            );
            """
        )
        await db.commit()


async def get_client() -> httpx.AsyncClient:
    if not MERAKI_API_KEY:
        raise HTTPException(status_code=500, detail="MERAKI_API_KEY manquante")
    headers = {
        "X-Cisco-Meraki-API-Key": MERAKI_API_KEY,
        "Accept": "application/json",
    }
    timeout = httpx.Timeout(60.0, connect=15.0)
    return httpx.AsyncClient(base_url=MERAKI_BASE_URL, headers=headers, timeout=timeout)


async def meraki_get_paginated(client: httpx.AsyncClient, path: str):
    items = []
    next_url: Optional[str] = path
    while next_url:
        response = await client.get(next_url)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, list):
            items.extend(data)
        else:
            return data
        next_url = None
        link = response.headers.get("Link", "")
        if 'rel="next"' in link:
            for part in link.split(","):
                if 'rel="next"' in part:
                    start = part.find("<")
                    end = part.find(">")
                    if start != -1 and end != -1:
                        url = part[start + 1:end]
                        next_url = url.replace(MERAKI_BASE_URL, "") if url.startswith(MERAKI_BASE_URL) else url
                        break
    return items


async def fetch_organizations() -> list:
    async with await get_client() as client:
        return await meraki_get_paginated(client, "/organizations")


async def fetch_organization_devices(organization_id: str) -> list:
    async with await get_client() as client:
        return await meraki_get_paginated(client, f"/organizations/{organization_id}/devices")


async def upsert_organizations(orgs: list) -> None:
    now = utc_now()
    async with aiosqlite.connect(DB_PATH) as db:
        for org in orgs:
            await db.execute(
                """
                INSERT INTO organizations (id, name, url, api_enabled, licensing_model, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    url=excluded.url,
                    api_enabled=excluded.api_enabled,
                    licensing_model=excluded.licensing_model,
                    updated_at=excluded.updated_at
                """,
                (
                    org.get("id"),
                    org.get("name"),
                    org.get("url"),
                    1 if org.get("api", {}).get("enabled") else 0,
                    org.get("licensing", {}).get("model"),
                    now,
                ),
            )
        await db.commit()


async def replace_organization_devices(organization_id: str, devices: list) -> None:
    now = utc_now()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM devices WHERE organization_id = ?", (organization_id,))
        for device in devices:
            await db.execute(
                """
                INSERT INTO devices (
                    serial, organization_id, name, model, product_type, network_id,
                    mac, lan_ip, firmware, status, tags, details_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    device.get("serial"),
                    organization_id,
                    device.get("name"),
                    device.get("model"),
                    device.get("productType"),
                    device.get("networkId"),
                    device.get("mac"),
                    device.get("lanIp"),
                    device.get("firmware"),
                    device.get("status"),
                    ", ".join(device.get("tags", [])) if isinstance(device.get("tags"), list) else device.get("tags"),
                    str(device),
                    now,
                ),
            )
        await db.execute(
            """
            UPDATE organizations
            SET device_count = ?, last_synced_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (len(devices), now, now, organization_id),
        )
        await db.execute(
            """
            INSERT INTO sync_state (key, value, updated_at)
            VALUES ('last_global_sync', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (now, now),
        )
        await db.commit()


async def refresh_all() -> dict:
    orgs = await fetch_organizations()
    await upsert_organizations(orgs)
    refreshed = 0
    for org in orgs:
        org_id = org.get("id")
        if not org_id:
            continue
        devices = await fetch_organization_devices(org_id)
        await replace_organization_devices(org_id, devices)
        refreshed += 1
        await asyncio.sleep(0.12)
    return {"organizations": len(orgs), "refreshed": refreshed, "timestamp": utc_now()}


async def refresh_one(organization_id: str) -> dict:
    devices = await fetch_organization_devices(organization_id)
    await replace_organization_devices(organization_id, devices)
    return {"organization_id": organization_id, "devices": len(devices), "timestamp": utc_now()}


async def get_dashboard_rows() -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT id, name, device_count, last_synced_at, licensing_model
            FROM organizations
            ORDER BY name COLLATE NOCASE ASC
            """
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_last_sync() -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT value FROM sync_state WHERE key = 'last_global_sync'")
        row = await cursor.fetchone()
        return row[0] if row else None


@app.on_event("startup")
async def startup_event() -> None:
    await init_db()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM organizations")
        count = (await cursor.fetchone())[0]
    if count == 0 and MERAKI_API_KEY:
        try:
            await refresh_all()
        except Exception:
            pass


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    rows = await get_dashboard_rows()
    last_sync = await get_last_sync()
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "rows": rows,
            "last_sync": last_sync,
            "api_configured": bool(MERAKI_API_KEY),
        },
    )


@app.get("/api/organizations")
async def api_organizations():
    return JSONResponse({"items": await get_dashboard_rows(), "last_sync": await get_last_sync()})


@app.post("/api/refresh")
async def api_refresh():
    return JSONResponse(await refresh_all())


@app.post("/api/refresh/{organization_id}")
async def api_refresh_one(organization_id: str):
    return JSONResponse(await refresh_one(organization_id))
