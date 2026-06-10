from datetime import datetime, timezone
from app.core.database import supabase

def process_weight_event(device_id: str, payload: dict):
    weight_g = payload.get("weight_g", 0)
    delta_g = payload.get("delta_g", 0)
    compartment = payload.get("compartment", 1)
    timestamp = payload.get("timestamp", datetime.now(timezone.utc).isoformat())

    event_type = "item_removed" if delta_g < 0 else "item_added"

    print(f"[{device_id}] {event_type} | Weight: {weight_g}g | Delta: {delta_g}g")

    # Store event in Supabase
    supabase.table("events").insert({
        "device_id": device_id,
        "weight_g": weight_g,
        "delta_g": delta_g,
        "compartment": compartment,
        "event_type": event_type,
        "timestamp": timestamp,
        "raw_payload": payload,
    }).execute()

    # Check low stock against registered items
    check_low_stock(device_id, compartment, weight_g)

def check_low_stock(device_id: str, compartment: int, current_weight: float):
    result = supabase.table("items")\
        .select("*")\
        .eq("device_id", device_id)\
        .execute()

    for item in result.data:
        if item["unit_weight_g"] and item["unit_weight_g"] > 0:
            estimated_qty = int(current_weight / item["unit_weight_g"])
            threshold = item.get("low_stock_threshold", 3)
            if estimated_qty <= threshold:
                print(f"LOW STOCK: {item['name']} — ~{estimated_qty} units left")