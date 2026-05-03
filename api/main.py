"""
api/main.py — FastAPI application entry point
═══════════════════════════════════════════════════

This file creates the FastAPI app instance and mounts:
  - Health check endpoint (for Docker / monitoring)
  - Webhook routers (Twilio callbacks)
  - Admin API routers (SIS sync, dashboard data)

Teaching note: FastAPI is "async-first". Every endpoint that touches
PostgreSQL will use `async def` and async SQLAlchemy sessions.
This lets one Uvicorn worker handle hundreds of concurrent requests
without blocking — critical on a Raspberry Pi with limited cores.
═══════════════════════════════════════════════════
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, status
from fastapi.responses import JSONResponse

# Import routers as we build them (Step 11+, currently stubs)
# from api.webhooks import twilio_router
# from api.admin import admin_router


# ── Lifespan: startup / shutdown events ──
# FastAPI 0.100+ uses lifespan context managers instead of @app.on_event.
# This is where we initialize database connection pools, verify Redis,
# and run startup health checks.
@asynccontextmanager
async def lifespan(app: FastAPI):
    # STARTUP
    # TODO (Step 4+): initialize async engine, verify Redis ping
    yield
    # SHUTDOWN
    # TODO (Step 4+): close database pool, flush logs


app = FastAPI(
    title="SMS Tuition Agent",
    description="Headless SMS-first tuition reminder and payment agent",
    version="0.1.0",
    lifespan=lifespan,
)


# ── Health Check ──
# Docker and monitoring tools (like Uptime Kuma) poll this endpoint.
# It must return 200 ONLY when the app AND its dependencies are healthy.
@app.get("/health", tags=["health"])
async def health_check():
    """
    Returns 200 if API is running.
    Step 4+: also checks DB and Redis connectivity.
    """
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "status": "healthy",
            "version": "0.1.0",
            # Step 4+: add "db": "connected", "redis": "connected"
        },
    )


# ── Mount Routers (as we build them) ──
# app.include_router(twilio_router, prefix="/webhooks", tags=["webhooks"])
# app.include_router(admin_router, prefix="/admin", tags=["admin"])


# ── Root redirect ──
@app.get("/", include_in_schema=False)
async def root():
    return {"message": "SMS Tuition Agent — see /docs for API documentation"}
