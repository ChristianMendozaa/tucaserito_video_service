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
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:3001", "https://www.tucaserito.com"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(video_router, prefix="/api/v1/video", tags=["video"])

@app.get("/health")
def health_check():
    return {"status": "ok"}
