"""Modem status sensors."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    SIGNAL_STRENGTH_DECIBELS,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import UlteConfigEntry, UlteCoordinator
from .entity import UlteEntity
from .parser import REGISTRATION, ModemStatus


@dataclass(frozen=True, kw_only=True)
class UlteSensorDescription(SensorEntityDescription):
    value_fn: Callable[[ModemStatus], Any]
    attrs_fn: Callable[[UlteCoordinator], dict[str, Any]] | None = None


SENSORS: tuple[UlteSensorDescription, ...] = (
    UlteSensorDescription(
        key="rsrp",
        translation_key="rsrp",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.rsrp,
    ),
    UlteSensorDescription(
        key="rsrq",
        translation_key="rsrq",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.rsrq,
    ),
    UlteSensorDescription(
        key="sinr",
        translation_key="sinr",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.sinr,
    ),
    UlteSensorDescription(
        key="rssi",
        translation_key="rssi",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.rssi,
    ),
    UlteSensorDescription(
        key="operator",
        translation_key="operator",
        value_fn=lambda s: s.operator,
    ),
    UlteSensorDescription(
        key="network_type",
        translation_key="network_type",
        value_fn=lambda s: s.system_mode or s.access_technology,
    ),
    UlteSensorDescription(
        key="band",
        translation_key="band",
        value_fn=lambda s: s.band,
        attrs_fn=lambda c: {"bandwidth": c.data.bandwidth},
    ),
    UlteSensorDescription(
        key="registration",
        translation_key="registration",
        device_class=SensorDeviceClass.ENUM,
        options=list(REGISTRATION.values()),
        value_fn=lambda s: s.registration,
    ),
    UlteSensorDescription(
        key="sms_on_sim",
        translation_key="sms_on_sim",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.sms_used,
        attrs_fn=lambda c: {
            "capacity": c.data.sms_total,
            "waiting_multipart_parts": c.waiting_parts,
            "last_delete_error": c.last_delete_error,
        },
    ),
    UlteSensorDescription(
        key="temperature",
        translation_key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.temperature,
    ),
    UlteSensorDescription(
        key="cell_id",
        translation_key="cell_id",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda s: s.cell_id,
        attrs_fn=lambda c: {"tac": c.data.tac},
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UlteConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    entities: list[SensorEntity] = [UlteSensor(entry, d) for d in SENSORS]
    entities.append(PhoneNumberSensor(entry))
    async_add_entities(entities)


class UlteSensor(UlteEntity, SensorEntity):
    entity_description: UlteSensorDescription

    def __init__(self, entry: UlteConfigEntry, description: UlteSensorDescription) -> None:
        super().__init__(entry, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator)


class PhoneNumberSensor(UlteEntity, SensorEntity):
    """Own number from the SIM (AT+CNUM), read once at setup."""

    _attr_translation_key = "phone_number"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, entry: UlteConfigEntry) -> None:
        super().__init__(entry, "phone_number")
        self._attr_native_value = entry.runtime_data.info.own_number
