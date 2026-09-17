"""Base entity shared by every Always Full platform."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import AlwaysFullCoordinator

if TYPE_CHECKING:
    from homeassistant.helpers.entity import EntityDescription

    from .coordinator import BowlData


class AlwaysFullEntity(CoordinatorEntity[AlwaysFullCoordinator]):
    """Common identity, device info and availability for one bowl."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AlwaysFullCoordinator,
        device_id: str,
        description: EntityDescription,
    ) -> None:
        """Bind this entity to one bowl and one entity description."""
        super().__init__(coordinator)
        self.entity_description = description
        self._device_id = device_id
        self._attr_unique_id = f"{device_id}_{description.key}"

    @property
    def bowl(self) -> BowlData | None:
        """Return this entity's bowl from the last poll, or `None` if it vanished."""
        return (self.coordinator.data or {}).get(self._device_id)

    @property
    def device_info(self) -> DeviceInfo:
        """Describe the physical bowl this entity belongs to.

        `identifiers` is keyed on the VENDOR's device id, never on anything
        derived from the config entry id. A config entry gets a fresh id
        every time the integration is removed and re-added; keying the
        device on it would create a brand-new device and orphan every
        recorder history row the user had built up.
        """
        bowl = self.bowl
        info = DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            manufacturer=MANUFACTURER,
        )
        if bowl is None:
            return info
        if bowl.state.device_name:
            info["name"] = bowl.state.device_name
        info["model"] = f'{bowl.state.bowl_size_inches}" Bowl'
        if bowl.firmware_version:
            info["sw_version"] = bowl.firmware_version
        return info

    @property
    def available(self) -> bool:
        """Available only while the last poll worked AND this bowl still exists.

        Deliberately not `super().available`: a bowl removed from the
        account keeps its entities registered but must stop reporting
        stale values as if they were live.
        """
        return self.coordinator.last_update_success and self._device_id in (
            self.coordinator.data or {}
        )
