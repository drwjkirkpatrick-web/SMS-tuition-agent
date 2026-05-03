"""
api/main.py — FastAPI application entry point (Step 4 update)
═══════════════════════════════════════════════════

Now with database and Redis wired into the lifespan manager.
═══════════════════════════════════════════════════
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, status
from fastapi.responses import JSONResponse

from infra.database import close_db, init_db
from infra.redis_pool import close_redis, ping_redis
from infra.settings import get_settings

# Import routers as we build them (Step 11+, currently stubs)
# from api.webhooks import twilio_router
# from api.admin import admin_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # STARTUP
    settings = get_settings()
    
    # Initialize database tables (dev only; production uses Alembic)
    if settings.app_env == "development":
        await init_db()
    
    # Verify Redis connectivity
    redis_ok = await ping_redis()
    if not redis_ok:
        raise RuntimeError("Redis is unreachable — cannot start")
    
    yield
    
    # SHUTDOWN
    await close_db()
    await close_redis()


app = FastAPI(
    title="SMS Tuition Agent",
    description="Headless SMS-first tuition reminder and payment agent",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health", tags=["health"])
async def health_check():
    """
    Returns 200 only if API, DB, and Redis are all healthy.
    Celery worker health is checked separately (Beat/Worker processes).
    """
    from infra.database import async_engine
    from sqlalchemy import text
    
    # Check DB
    try:
        async with async_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception:
        db_status = "disconnected"
    
    # Check Redis
    redis_status = "connected" if await ping_redis() else "disconnected"
    
    overall = "healthy" if db_status == "connected" and redis_status == "connected" else "unhealthy"
    code = status.HTTP_200_OK if overall == "healthy" else status.HTTP_503_SERVICE_UNAVAILABLE
    
    return JSONResponse(
        status_code=code,
        content={
            "status": overall,
            "version": "0.1.0",
            "db": db_status,
            "redis": redis_status,
        },
    )


# app.include_router(twilio_router, prefix="/webhooks", tags=["webhooks"])
# app.include_router(admin_router, prefix="/admin", tags=["admin"])


@app.get("/", include_in_schema=False)
async def root():
    return {"message": "SMS Tuition Agent — see /docs for API documentation"}
