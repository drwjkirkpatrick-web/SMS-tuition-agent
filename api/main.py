"""
api/main.py — FastAPI application entry point
═══════════════════════════════════════════════════

Step 13 update: webhook routers mounted.
v2 update: CORS, admin router, startup validation, PII logging.
═══════════════════════════════════════════════════
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.admin import router as admin_router
from api.webhooks.twilio import router as twilio_webhook_router
from infra.database import close_db, init_db
from infra.redis_pool import close_redis, ping_redis
from infra.settings import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    # S2: Enforce admin token configuration in production
    if settings.app_env == "production" and not settings.admin_token_hash:
        raise RuntimeError(
            "ADMIN_TOKEN_HASH must be set in production environment"
        )

    # S10: Set up PII masking on the root logger
    from infra.logging_filter import setup_pii_logging
    setup_pii_logging()

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
    version="0.2.0",
    lifespan=lifespan,
)

# S6: CORS configuration
_settings = get_settings()
_allowed_origins = [
    o.strip() for o in _settings.cors_allowed_origins.split(",") if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
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
        content={"status": overall, "version": "0.2.0", "db": db_status, "redis": redis_status},
    )


# Mount webhook router
app.include_router(twilio_webhook_router, prefix="/webhooks", tags=["webhooks"])
# Mount admin router
app.include_router(admin_router, prefix="/admin", tags=["admin"])


@app.get("/", include_in_schema=False)
async def root():
    return {"message": "SMS Tuition Agent — see /docs for API documentation"}
