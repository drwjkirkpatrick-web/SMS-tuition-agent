"""
api/main.py — FastAPI application entry point (Step 13 update)
═══════════════════════════════════════════════════

Now with webhook routers mounted.
═══════════════════════════════════════════════════
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, status
from fastapi.responses import JSONResponse

from api.webhooks.twilio import router as twilio_webhook_router
from infra.database import close_db, init_db
from infra.redis_pool import close_redis, ping_redis
from infra.settings import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if settings.app_env == "development":
        await init_db()
    redis_ok = await ping_redis()
    if not redis_ok:
        raise RuntimeError("Redis is unreachable")
    yield
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
    from infra.database import async_engine
    from sqlalchemy import text
    
    try:
        async with async_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception:
        db_status = "disconnected"
    
    redis_status = "connected" if await ping_redis() else "disconnected"
    overall = "healthy" if db_status == "connected" and redis_status == "connected" else "unhealthy"
    code = status.HTTP_200_OK if overall == "healthy" else status.HTTP_503_SERVICE_UNAVAILABLE
    
    return JSONResponse(
        status_code=code,
        content={"status": overall, "version": "0.1.0", "db": db_status, "redis": redis_status},
    )


# Mount webhook router
app.include_router(twilio_webhook_router, prefix="/webhooks", tags=["webhooks"])


@app.get("/", include_in_schema=False)
async def root():
    return {"message": "SMS Tuition Agent — see /docs for API documentation"}
