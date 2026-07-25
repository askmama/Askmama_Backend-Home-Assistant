"""Canonical device identity for incoming scale events.

The firmware reports its id in two independent places that can disagree: the
MQTT topic segment (askmama/<id>/weight_event) and the device_id field inside
the JSON payload. ESPHome configs have shipped with those two out of sync, so
the backend agrees them before use and refuses events where they still differ.
A mismatch means we cannot tell which device an event came from — and since
device_id now resolves the owner, guessing files the event under the wrong
user's account.
"""

import re

# Canonical form is the bare device suffix, e.g. "scale-v2". Existing devices
# and items rows are keyed this way (they were populated from the topic
# segment), so stripping the vendor prefix keeps the firmware's
# "askmama-scale-v2" compatible with data already in Supabase.
_VENDOR_PREFIX = "askmama-"
_VALID_DEVICE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")


class DeviceIdentityError(ValueError):
    """An event's device identity is missing, malformed, or self-inconsistent."""


def canonical_device_id(raw) -> str:
    if not isinstance(raw, str):
        raise DeviceIdentityError(f"device id must be a string, got {type(raw).__name__}")

    device_id = raw.strip().lower()
    if device_id.startswith(_VENDOR_PREFIX):
        device_id = device_id[len(_VENDOR_PREFIX):]

    if not _VALID_DEVICE_ID.match(device_id):
        raise DeviceIdentityError(f"malformed device id: {raw!r}")
    return device_id


def resolve_device_id(topic: str, payload: dict) -> str:
    """Agree the device id across topic and payload, or refuse the event."""
    parts = topic.split("/")
    if len(parts) < 2:
        raise DeviceIdentityError(f"cannot read device id from topic {topic!r}")
    topic_id = canonical_device_id(parts[1])

    claimed = payload.get("device_id")
    if claimed is None:
        # Firmware built before the payload carried device_id; topic is all we have.
        return topic_id

    payload_id = canonical_device_id(claimed)
    if payload_id != topic_id:
        raise DeviceIdentityError(
            f"device id mismatch: topic says {topic_id!r}, payload says {payload_id!r} "
            "— refusing to attribute this event to either"
        )
    return topic_id
