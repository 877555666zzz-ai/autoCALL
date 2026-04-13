import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, BackgroundTasks, Request, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select, func, delete

from .dispatcher import process_new_lead, autodial_worker, schedule_autodial
from .sipuni_client import make_outbound_call
from .bitrix_client import get_lead
from .db import init_db, async_session_maker
from .models import Manager, AutodialQueue, CallLog

# ─── Логирование ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ─── Приложение ─────────────────────────────────────────────────────────────
app = FastAPI(title="AutoCall · Bitrix24 + Sipuni", version="2.0.0")
app.mount("/static", StaticFiles(directory="static"), name="static")


# ─── Middleware ──────────────────────────────────────────────────────────────
@app.middleware("http")
async def log_requests(request: Request, call_next):
    # Пропускаем все запросы; whitelist можно включить тут
    response = await call_next(request)
    return response


# ─── Startup ─────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup_event():
    await init_db()

    # Добавляем дефолтных менеджеров только если таблица пуста
    async with async_session_maker() as session:
        result = await session.execute(select(func.count()).select_from(Manager))
        count = result.scalar_one()
        if count == 0:
            session.add_all([
                Manager(id=11, name="Тамерлан", sipnumber="999",
                        online=True, missed=0, accepted_calls=0),
                Manager(id=12, name="Арсен", sipnumber="236",
                        online=True, missed=0, accepted_calls=0),
            ])
            await session.commit()
            logger.info("[startup] Добавлены дефолтные менеджеры")

    asyncio.create_task(autodial_worker())
    logger.info("[startup] AutoCall запущен")


# ─── Root / Dashboard ────────────────────────────────────────────────────────
@app.get("/", include_in_schema=False)
async def root():
    return FileResponse("static/dashboard.html")


# ─── Health check (Railway использует его) ──────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "ts": datetime.utcnow().isoformat()}


# ─── Менеджеры ───────────────────────────────────────────────────────────────

class ManagerCreate(BaseModel):
    name: str
    sipnumber: str
    online: bool = True


class ManagerUpdate(BaseModel):
    name: Optional[str] = None
    sipnumber: Optional[str] = None
    online: Optional[bool] = None


def _mgr_dict(m: Manager) -> dict:
    return {
        "id": m.id,
        "name": m.name,
        "sipnumber": m.sipnumber,
        "online": m.online,
        "missed": m.missed or 0,
        "accepted_calls": m.accepted_calls or 0,
        "status": "НА ЛИНИИ" if m.online else "НЕ АКТИВЕН",
    }


@app.get("/managers")
async def list_managers():
    """Список всех менеджеров."""
    async with async_session_maker() as session:
        result = await session.execute(select(Manager).order_by(Manager.id))
        managers = result.scalars().all()
    return [_mgr_dict(m) for m in managers]


@app.post("/managers", status_code=201)
async def create_manager(data: ManagerCreate):
    """Добавить нового менеджера."""
    async with async_session_maker() as session:
        mgr = Manager(
            name=data.name,
            sipnumber=data.sipnumber,
            online=data.online,
            missed=0,
            accepted_calls=0,
        )
        session.add(mgr)
        await session.commit()
        await session.refresh(mgr)
    logger.info("[manager] Добавлен: %s (ext=%s)", mgr.name, mgr.sipnumber)
    return _mgr_dict(mgr)


@app.put("/managers/{manager_id}")
async def update_manager(manager_id: int, data: ManagerUpdate):
    """Обновить данные менеджера."""
    async with async_session_maker() as session:
        mgr = await session.get(Manager, manager_id)
        if not mgr:
            raise HTTPException(status_code=404, detail="manager_not_found")
        if data.name is not None:
            mgr.name = data.name
        if data.sipnumber is not None:
            mgr.sipnumber = data.sipnumber
        if data.online is not None:
            mgr.online = data.online
            if data.online:
                mgr.missed = 0
        await session.commit()
        await session.refresh(mgr)
    return _mgr_dict(mgr)


@app.delete("/managers/{manager_id}")
async def delete_manager(manager_id: int):
    """Удалить менеджера."""
    async with async_session_maker() as session:
        mgr = await session.get(Manager, manager_id)
        if not mgr:
            raise HTTPException(status_code=404, detail="manager_not_found")
        await session.delete(mgr)
        await session.commit()
    logger.info("[manager] Удалён id=%d", manager_id)
    return {"status": "deleted", "id": manager_id}


@app.post("/managers/{manager_id}/online")
async def set_manager_online(manager_id: int):
    """Поставить менеджера НА ЛИНИИ."""
    async with async_session_maker() as session:
        mgr = await session.get(Manager, manager_id)
        if not mgr:
            raise HTTPException(status_code=404, detail="manager_not_found")
        mgr.online = True
        mgr.missed = 0
        await session.commit()
        await session.refresh(mgr)
    return _mgr_dict(mgr)


@app.post("/managers/{manager_id}/offline")
async def set_manager_offline(manager_id: int):
    """Поставить менеджера НЕ АКТИВЕН."""
    async with async_session_maker() as session:
        mgr = await session.get(Manager, manager_id)
        if not mgr:
            raise HTTPException(status_code=404, detail="manager_not_found")
        mgr.online = False
        await session.commit()
        await session.refresh(mgr)
    return _mgr_dict(mgr)


# ─── Логи и очередь ──────────────────────────────────────────────────────────
@app.get("/logs")
async def get_logs(limit: int = 50):
    """Последние логи звонков + активная очередь автодозвона."""
    async with async_session_maker() as session:
        logs_result = await session.execute(
            select(CallLog).order_by(CallLog.id.desc()).limit(limit)
        )
        logs = logs_result.scalars().all()

        queue_result = await session.execute(
            select(AutodialQueue).where(
                AutodialQueue.state.in_(["SCHEDULED", "IN_PROGRESS"])
            ).order_by(AutodialQueue.next_call_time)
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
                "next_call_time": q.next_call_time.isoformat() if q.next_call_time else None,
                "state": q.state,
            }
            for q in queue
        ],
    }


# ─── Аналитика ───────────────────────────────────────────────────────────────
@app.get("/analytics")
async def get_analytics(days: int = 7):
    """
    Сводная аналитика за последние N дней.
    Возвращает: общее кол-во звонков, конверсию, статистику по менеджерам.
    """
    since = datetime.utcnow() - timedelta(days=days)

    async with async_session_maker() as session:
        # Все логи за период
        logs_result = await session.execute(
            select(CallLog).where(CallLog.timestamp >= since)
        )
        logs = logs_result.scalars().all()

        # Менеджеры
        mgr_result = await session.execute(select(Manager).order_by(Manager.id))
        managers = mgr_result.scalars().all()

        # Всего в очереди (исторически)
        queue_result = await session.execute(
            select(func.count()).select_from(AutodialQueue)
        )
        total_queued = queue_result.scalar_one()

    total = len(logs)
    connected = sum(1 for l in logs if l.status == "connected")
    no_answer = sum(1 for l in logs if l.status == "no_answer")
    no_managers = sum(1 for l in logs if l.status == "no_managers")
    initial_calls = sum(1 for l in logs if l.type == "initial")
    autodial_calls = sum(1 for l in logs if l.type == "autodial")
    conversion_rate = round(connected / total * 100, 1) if total > 0 else 0.0

    return {
        "period_days": days,
        "total_calls": total,
        "connected": connected,
        "no_answer": no_answer,
        "no_managers": no_managers,
        "initial_calls": initial_calls,
        "autodial_calls": autodial_calls,
        "conversion_rate": conversion_rate,
        "total_ever_queued": total_queued,
        "managers": [
            {
                "id": m.id,
                "name": m.name,
                "sipnumber": m.sipnumber,
                "online": m.online,
                "accepted_calls": m.accepted_calls or 0,
                "missed": m.missed or 0,
            }
            for m in managers
        ],
    }


# ─── Тестовые эндпоинты ──────────────────────────────────────────────────────
@app.get("/test/sipuni_call")
async def test_sipuni_call(manager_id: int, client_phone: str):
    """Тестовый прямой звонок через Sipuni."""
    async with async_session_maker() as session:
        mgr = await session.get(Manager, manager_id)
        if not mgr:
            raise HTTPException(status_code=404, detail="manager_not_found")
        result = await make_outbound_call(mgr.sipnumber, client_phone)
        return {"manager": _mgr_dict(mgr), "sipuni_response": result}


@app.post("/test/lead")
async def test_lead(lead_id: int, client_phone: str, background_tasks: BackgroundTasks):
    """Тестовый запуск обработки лида напрямую (без Bitrix webhook)."""
    background_tasks.add_task(process_new_lead, lead_id, client_phone)
    return {"status": "accepted", "lead_id": lead_id, "phone": client_phone}


# ─── Bitrix Webhook ──────────────────────────────────────────────────────────
@app.post("/bitrix/webhook/lead")
async def bitrix_lead_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Вебхук из Bitrix24 при создании нового лида.
    Поддерживает JSON (Swagger/тесты) и form-data (Bitrix).
    """
    lead_id: Optional[int] = None

    # 1. Пробуем JSON
    try:
        body = await request.json()
    except Exception:
        body = None

    if isinstance(body, dict):
        data = body.get("data") or {}
        fields = data.get("FIELDS") or {}
        raw_id = fields.get("ID") or body.get("lead_id")
        if raw_id:
            try:
                lead_id = int(raw_id)
            except (ValueError, TypeError):
                pass

    # 2. Иначе — form-data от Bitrix
    if lead_id is None:
        try:
            form = await request.form()
            raw_id = (
                form.get("data[FIELDS][ID]")
                or form.get("FIELDS[ID]")
                or form.get("lead_id")
            )
            if raw_id:
                lead_id = int(raw_id)
        except Exception:
            pass

    if lead_id is None:
        logger.warning("[webhook] lead_id не найден в запросе")
        return JSONResponse(
            status_code=400,
            content={"status": "error", "reason": "lead_id_not_found"},
        )

    logger.info("[webhook] Получен новый лид #%d", lead_id)

    async def process_lead_task(l_id: int):
        try:
            lead_data = await get_lead(l_id)
            result = lead_data.get("result") or {}
            phones = result.get("PHONE") or []
            client_phone = phones[0].get("VALUE", "") if phones else ""

            if not client_phone:
                logger.warning("[webhook] Лид #%d: телефон не найден", l_id)
                return

            await process_new_lead(l_id, client_phone)
        except Exception as e:
            logger.error("[webhook] Ошибка обработки лида #%d: %s", l_id, e)

    background_tasks.add_task(process_lead_task, lead_id)
    return {"status": "accepted", "lead_id": lead_id}
