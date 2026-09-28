from fastapi import APIRouter

from app.api.routes import agents, analyze, health, markets, twelve_data, webhooks

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(agents.router)
api_router.include_router(markets.router)
api_router.include_router(analyze.router)
api_router.include_router(webhooks.router)
api_router.include_router(twelve_data.router)
