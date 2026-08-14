"""Coordinate vehicles that are visible to more than one account."""

from collections.abc import MutableMapping, Sequence
from typing import Any


def claim_vehicles(
    claims: MutableMapping[str, str],
    entry_id: str,
    vehicles: Sequence[Any],
) -> tuple[list[Any], list[Any]]:
    """Return vehicles owned by this entry and vehicles owned elsewhere."""
    claimed = []
    conflicts = []
    for vehicle in vehicles:
        owner = claims.get(vehicle.vin)
        if owner is not None and owner != entry_id:
            conflicts.append(vehicle)
            continue
        claims[vehicle.vin] = entry_id
        claimed.append(vehicle)
    return claimed, conflicts


def release_vehicle_claims(claims: MutableMapping[str, str], entry_id: str) -> None:
    """Release every vehicle owned by a config entry."""
    for vin in [vin for vin, owner in claims.items() if owner == entry_id]:
        del claims[vin]


def release_selected_vehicle_claims(
    claims: MutableMapping[str, str], entry_id: str, vins: set[str]
) -> None:
    """Release selected VINs only when they belong to the config entry."""
    for vin in vins:
        if claims.get(vin) == entry_id:
            del claims[vin]
