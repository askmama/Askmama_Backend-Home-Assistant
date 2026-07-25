import time
from datetime import datetime, timezone
from app.core.database import supabase

# Cache device_id -> (user_id, cached_at) so we don't hit Supabase on every
# weight event. Entries expire after _OWNER_TTL_S so a release + re-claim by a
# different user is picked up without a backend restart.
_OWNER_TTL_S = 300
_owner_cache: dict[str, tuple[str, float]] = {}

def _owner_for_device(device_id: str) -> str | None:
    hit = _owner_cache.get(device_id)
    if hit and time.monotonic() - hit[1] < _OWNER_TTL_S:
        return hit[0]
    res = supabase.table("devices").select("user_id").eq("device_id", device_id).limit(1).execute()
    user_id = res.data[0]["user_id"] if res.data else None
    # Only cache resolved owners; keep retrying lookups for still-unclaimed devices.
    if user_id is not None:
        _owner_cache[device_id] = (user_id, time.monotonic())
    else:
        _owner_cache.pop(device_id, None)
    return user_id

def process_weight_event(device_id: str, payload: dict):
    weight_g = payload.get("weight_g", 0)
    delta_g = payload.get("delta_g", 0)
    compartment = payload.get("compartment", 1)
    timestamp = payload.get("timestamp", datetime.now(timezone.utc).isoformat())

    event_type = "item_removed" if delta_g < 0 else "item_added"
    user_id = _owner_for_device(device_id)  # None if device not yet claimed

    print(f"[{device_id}] {event_type} | Weight: {weight_g}g | Delta: {delta_g}g | owner: {user_id}")

    # Store event in Supabase (service key bypasses RLS). user_id stamps ownership;
    # an unclaimed device logs with user_id=None (requires events.user_id nullable,
    # migration 0006) — invisible under RLS until claim_device() adopts the rows.
    supabase.table("events").insert({
        "device_id": device_id,
        "user_id": user_id,
        "weight_g": weight_g,
        "delta_g": delta_g,
        "compartment": compartment,
        "event_type": event_type,
        "timestamp": timestamp,
        "raw_payload": payload,
    }).execute()

    # Check low stock against items in this scale's bin
    check_low_stock(device_id, weight_g)

def check_low_stock(device_id: str, current_weight: float):
    # One scale == one bin, so the device_id resolves to a single bin. Items
    # reach a scale through item -> items_to_bins -> bin -> device_id, so we
    # look up the bin first, then the items mapped into it.
    bin_res = supabase.table("bins").select("id").eq("device_id", device_id).limit(1).execute()
    if not bin_res.data:
        return  # scale not yet mounted in a bin
    bin_id = bin_res.data[0]["id"]

    # Pull the items mapped into this bin (nested select via the FK to items).
    links = supabase.table("items_to_bins")\
        .select("quantity, items(*)")\
        .eq("bin_id", bin_id)\
        .execute()

    for link in links.data:
        item = link.get("items")
        # NOTE: the scale reports total bin weight, so per-item estimation is only
        # meaningful when a bin holds a single item. This mirrors the prior behavior.
        if item and item.get("unit_weight_g") and item["unit_weight_g"] > 0:
            estimated_qty = int(current_weight / item["unit_weight_g"])
            threshold = item.get("low_stock_threshold", 3)
            if estimated_qty <= threshold:
                print(f"LOW STOCK: {item['name']} — ~{estimated_qty} units left")
