import os
import json
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded
from slowapi import _rate_limit_exceeded_handler

from app.api.endpoints import router as video_router
from app.core.rate_limit import limiter

app = FastAPI(
    title="Video Generation Service",
    description="Service for generating videos using Vertex AI Veo-3.1 API dynamically.",
    version="1.0.0"
)

# Attach slowapi rate limiter to app
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Set up CORS for integration with other services if needed
allowed_origins_env = os.getenv("ALLOWED_ORIGINS", '["http://localhost:3000", "https://www.tucaserito.com"]')
try:
    origins = json.loads(allowed_origins_env)
except Exception:
    origins = []

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept", "Origin"],
)

app.include_router(video_router, prefix="/api/v1/video", tags=["video"])

@app.get("/health")
def health_check():
    return {"status": "ok"}
