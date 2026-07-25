from fastapi import APIRouter, HTTPException
from app.core.database import supabase

router = APIRouter(prefix="/api/v1")

# NOTE: the backend uses the Supabase service key, which bypasses RLS. These
# endpoints are therefore NOT user-scoped on their own — callers must pass the
# owning user_id where the table requires it (shelves, bins). Mirrors the
# existing register_item() dict-passthrough style.

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

# --- Shelves ---------------------------------------------------------------

@router.get("/shelves")
def list_shelves(user_id: str | None = None):
    q = supabase.table("shelves").select("*").order("created_at")
    if user_id:
        q = q.eq("user_id", user_id)
    return q.execute().data

@router.post("/shelves")
def create_shelf(shelf: dict):
    # Requires user_id + name (shelves.user_id is NOT NULL).
    result = supabase.table("shelves").insert(shelf).execute()
    return result.data

@router.patch("/shelves/{shelf_id}")
def update_shelf(shelf_id: str, fields: dict):
    result = supabase.table("shelves").update(fields).eq("id", shelf_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Shelf not found")
    return result.data

@router.delete("/shelves/{shelf_id}")
def delete_shelf(shelf_id: str):
    # bins.shelf_id is ON DELETE SET NULL, so bins survive as unassigned.
    supabase.table("shelves").delete().eq("id", shelf_id).execute()
    return {"deleted": shelf_id}

# --- Bins (one scale == one bin) -------------------------------------------

@router.get("/bins")
def list_bins(shelf_id: str | None = None, user_id: str | None = None):
    q = supabase.table("bins").select("*").order("position")
    if shelf_id:
        q = q.eq("shelf_id", shelf_id)
    if user_id:
        q = q.eq("user_id", user_id)
    return q.execute().data

@router.post("/bins")
def create_bin(bin: dict):
    # Requires user_id + device_id (unique). shelf_id/position/name optional.
    result = supabase.table("bins").insert(bin).execute()
    return result.data

@router.patch("/bins/{bin_id}")
def update_bin(bin_id: str, fields: dict):
    # Reassign a shelf, reorder (position), or rename.
    result = supabase.table("bins").update(fields).eq("id", bin_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Bin not found")
    return result.data

@router.delete("/bins/{bin_id}")
def delete_bin(bin_id: str):
    supabase.table("bins").delete().eq("id", bin_id).execute()
    return {"deleted": bin_id}

# --- Item ↔ bin mapping (an item may live in several bins) ------------------

@router.get("/bins/{bin_id}/items")
def list_bin_items(bin_id: str):
    # Nested select pulls the joined item row via the FK.
    return supabase.table("items_to_bins")\
        .select("*, items(*)")\
        .eq("bin_id", bin_id)\
        .execute().data

@router.get("/items/{item_id}/bins")
def list_item_bins(item_id: str):
    return supabase.table("items_to_bins")\
        .select("*, bins(*)")\
        .eq("item_id", item_id)\
        .execute().data

@router.post("/items/{item_id}/bins")
def assign_item_to_bin(item_id: str, body: dict):
    # body: {"bin_id": ..., "quantity": ...}. unique(item_id, bin_id) means a
    # repeat assignment should update quantity rather than duplicate — upsert.
    row = {"item_id": item_id, "bin_id": body["bin_id"], "quantity": body.get("quantity")}
    result = supabase.table("items_to_bins")\
        .upsert(row, on_conflict="item_id,bin_id")\
        .execute()
    return result.data

@router.delete("/items/{item_id}/bins/{bin_id}")
def unassign_item_from_bin(item_id: str, bin_id: str):
    supabase.table("items_to_bins")\
        .delete()\
        .eq("item_id", item_id)\
        .eq("bin_id", bin_id)\
        .execute()
    return {"unassigned": {"item_id": item_id, "bin_id": bin_id}}