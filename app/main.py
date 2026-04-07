# app/main.py
from fastapi.staticfiles import StaticFiles
from fastapi import FastAPI, BackgroundTasks, Body, Request
from fastapi.responses import JSONResponse
import asyncio

from sqlalchemy import select

from .dispatcher import process_new_lead, autodial_worker
from .sipuni_client import make_outbound_call
from .bitrix_client import get_lead
from .db import init_db, async_session_maker
from .models import Manager, AutodialQueue, CallLog

app = FastAPI(title="Autocall Bitrix24 + Sipuni")
app.mount("/static", StaticFiles(directory="static"), name="static")

# ALLOWED_IPS = ["127.0.0.1"]  # временно не используем


@app.middleware("http")
async def check_whitelist(request: Request, call_next):
    # временно пропускаем всё, чтобы Bitrix мог стучаться снаружи
    return await call_next(request)

    client_host = request.client.host
    if client_host not in ALLOWED_IPS:
        return JSONResponse(
            status_code=403,
            content={"error": "Access denied"}
        )

    return await call_next(request)


@app.on_event("startup")
async def startup_event():
    await init_db()

    async with async_session_maker() as session:
        result = await session.execute(select(Manager))
        managers = result.scalars().all()
        if not managers:
            session.add_all([
                Manager(id=11, name="Тамерлан", sipnumber="999", online=True, missed=0, accepted_calls=0),
                Manager(id=12, name="Арсен",   sipnumber="236",   online=True, missed=0, accepted_calls=0),
            ])
            await session.commit()

    asyncio.create_task(autodial_worker())


@app.get("/test/sipuni_call")
async def test_sipuni_call(manager_id: int, client_phone: str):
    """Тестовый звонок через Sipuni напрямую"""
    async with async_session_maker() as session:
        mgr = await session.get(Manager, manager_id)
        if not mgr:
            return {"error": "manager_not_found"}

        result = await make_outbound_call(mgr.sipnumber, client_phone)
        return {
            "manager": {
                "id": mgr.id,
                "name": mgr.name,
                "sipnumber": mgr.sipnumber,
                "online": mgr.online,
                "missed": mgr.missed,
                "accepted_calls": mgr.accepted_calls,
            },
            "sipuni_response": result,
        }


@app.get("/managers")
async def list_managers():
    """Список всех менеджеров и их статусы"""
    async with async_session_maker() as session:
        result = await session.execute(select(Manager))
        managers = result.scalars().all()
        return [
            {
                "id": m.id,
                "name": m.name,
                "sipnumber": m.sipnumber,
                "online": m.online,
                "missed": m.missed,
                "accepted_calls": m.accepted_calls,
                "status": "НА ЛИНИИ" if m.online else "НЕ АКТИВЕН",
            }
            for m in managers
        ]


@app.post("/managers/{manager_id}/online")
async def set_manager_online(manager_id: int):
    """Поставить менеджера НА ЛИНИИ"""
    async with async_session_maker() as session:
        mgr = await session.get(Manager, manager_id)
        if not mgr:
            return {"status": "not_found"}

        mgr.online = True
        mgr.missed = 0
        await session.commit()
        await session.refresh(mgr)

        return {
            "status": "ok",
            "name": mgr.name,
            "online": mgr.online,
            "accepted_calls": mgr.accepted_calls,
        }


@app.post("/managers/{manager_id}/offline")
async def set_manager_offline(manager_id: int):
    """Поставить менеджера НЕ АКТИВЕН"""
    async with async_session_maker() as session:
        mgr = await session.get(Manager, manager_id)
        if not mgr:
            return {"status": "not_found"}

        mgr.online = False
        await session.commit()
        await session.refresh(mgr)

        return {
            "status": "ok",
            "name": mgr.name,
            "online": mgr.online,
            "accepted_calls": mgr.accepted_calls,
        }


@app.get("/logs")
async def get_logs():
    """Логи звонков и очередь автодозвона из БД"""
    async with async_session_maker() as session:
        logs_result = await session.execute(
            select(CallLog).order_by(CallLog.id.desc()).limit(50)
        )
        logs = logs_result.scalars().all()

        queue_result = await session.execute(
            select(AutodialQueue).where(
                AutodialQueue.state.in_(["SCHEDULED", "IN_PROGRESS"])
            )
        )
        queue = queue_result.scalars().all()

    return {
        "total_logs": len(logs),
        "logs": [
            {
                "id": l.id,
                "timestamp": l.timestamp.isoformat() if l.timestamp else None,
                "lead_id": l.lead_id,
                "phone": l.phone,
                "type": l.type,
                "status": l.status,
            }
            for l in logs
        ],
        "autodial_queue": [
            {
                "lead_id": q.lead_id,
                "phone": q.phone,
                "attempts": q.attempts,
                "next_call_time": q.next_call_time.isoformat(),
                "state": q.state,
            }
            for q in queue
        ],
    }


@app.post("/bitrix/webhook/lead")
async def bitrix_lead_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
):
    """Вебхук из Bitrix — новый лид (поддержка JSON и form-data)"""
    lead_id = None

    # Пытаемся сначала прочитать как JSON (Swagger / ручные тесты)
    try:
        body = await request.json()
    except Exception:
        body = None

    if isinstance(body, dict) and "data" in body:
        data = body.get("data") or {}
        fields = data.get("FIELDS") or {}
        lead_id_raw = fields.get("ID")
        if lead_id_raw:
            try:
                lead_id = int(lead_id_raw)
            except ValueError:
                lead_id = None
    else:
        # Если это не JSON (Bitrix шлёт form-data)
        form = await request.form()
        # В Bitrix исходящем вебхуке обычно приходит так:
        # event=ONCRMLEADADD
        # data[FIELDS][ID]=123
        lead_id_raw = form.get("data[FIELDS][ID]") or form.get("FIELDS[ID]")
        if lead_id_raw:
            try:
                lead_id = int(lead_id_raw)
            except ValueError:
                lead_id = None

    if lead_id is None:
        return {"status": "error", "reason": "lead_id_not_found_in_body"}

    async def process_lead_task(l_id: int):
        lead_data = await get_lead(l_id)
        result = lead_data.get("result") or {}

        client_phone = ""
        phones = result.get("PHONE") or []
        if phones and isinstance(phones, list):
            client_phone = phones[0].get("VALUE", "")

        if client_phone:
            await process_new_lead(l_id, client_phone)

    background_tasks.add_task(process_lead_task, lead_id)
    return {"status": "accepted", "lead_id": lead_id}
