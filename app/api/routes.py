from fastapi import APIRouter
from app.core.database import supabase

router = APIRouter(prefix="/api/v1")

@router.get("/scales/{device_id}/events")
def get_events(device_id: str, limit: int = 50):
    result = supabase.table("events")\
        .select("*")\
        .eq("device_id", device_id)\
        .order("timestamp", desc=True)\
        .limit(limit)\
        .execute()
    return result.data

@router.get("/scales/{device_id}/weight")
def get_current_weight(device_id: str):
    result = supabase.table("events")\
        .select("*")\
        .eq("device_id", device_id)\
        .order("timestamp", desc=True)\
        .limit(1)\
        .execute()
    return result.data[0] if result.data else {"message": "No data yet"}

@router.post("/items")
def register_item(item: dict):
    result = supabase.table("items").insert(item).execute()
    return result.data