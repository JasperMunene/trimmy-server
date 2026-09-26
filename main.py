import logging
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from prometheus_fastapi_instrumentator import Instrumentator
from slowapi.errors import RateLimitExceeded

from app.core.config import settings
from app.core.rate_limit import limiter
from app.core.storage import LocalStorage, get_storage
from app.workers.broker import publisher
from app.modules.auth.router import router as auth_router
from app.modules.business.router import discovery_router
from app.modules.business.router import options_router
from app.modules.payments.router import router as payments_router
from app.modules.business.router import router as business_router
from app.modules.business.router import team_router

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await publisher.start()
    yield
    await publisher.stop()


app = FastAPI(
    lifespan=lifespan,
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description="Trimmy backend API",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def rate_limit_exception_handler(request, exc: RateLimitExceeded):
    retry_after = exc.limit.limit.get_expiry()
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content={
            "error": "too_many_requests",
            "message": "Too many requests. Please try again later.",
            "retry_after_seconds": retry_after,
        },
        headers={"Retry-After": str(retry_after)},
    )


Instrumentator().instrument(app).expose(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router, prefix=settings.API_V1_STR)
# discovery_router must precede business_router: its GET /businesses/search
# would otherwise match the authed GET /businesses/{business_id} route.
app.include_router(discovery_router, prefix=settings.API_V1_STR)
app.include_router(business_router, prefix=settings.API_V1_STR)
app.include_router(team_router, prefix=settings.API_V1_STR)
app.include_router(options_router, prefix=settings.API_V1_STR)
app.include_router(payments_router, prefix=settings.API_V1_STR)

if isinstance(get_storage(), LocalStorage):
    # Dev only: serve locally stored uploads. With R2, files are served by R2 itself.
    os.makedirs(settings.MEDIA_ROOT, exist_ok=True)
    app.mount("/media", StaticFiles(directory=settings.MEDIA_ROOT), name="media")


@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.detail if isinstance(exc.detail, dict) else {"error": "unknown", "message": exc.detail},
        headers=getattr(exc, "headers", None),
    )


@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "trimmy-api", "version": settings.VERSION}


@app.get("/")
async def root():
    return {"app": settings.PROJECT_NAME, "version": settings.VERSION, "docs": "/docs"}
