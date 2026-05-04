"""Coordinator for the DMI Observation integration."""

from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    DMIConnectionError,
    DMIObservationClient,
    DMIStationNotFoundError,
    ObservationSnapshot,
)
from .const import (
    CONF_SCAN_INTERVAL,
    CONF_STATION_ID,
    DEFAULT_NAME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


class DMIObservationCoordinator(DataUpdateCoordinator[ObservationSnapshot]):
    """Coordinate DMI observation API updates."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        self.config_entry = entry
        config = {**entry.data, **entry.options}
        self.station_id = config[CONF_STATION_ID]
        self.client = DMIObservationClient(aiohttp_client.async_get_clientsession(hass))

        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=entry.title or DEFAULT_NAME,
            update_interval=timedelta(
                seconds=max(
                    int(config.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)),
                    MIN_SCAN_INTERVAL,
                )
            ),
        )

    async def _async_update_data(self) -> ObservationSnapshot:
        """Fetch the latest station data."""
        try:
            return await self.client.async_get_snapshot(self.station_id)
        except DMIStationNotFoundError as error:
            raise UpdateFailed(
                f"DMI station {self.station_id} could not be found"
            ) from error
        except DMIConnectionError as error:
            raise UpdateFailed(f"Could not fetch DMI data: {error}") from error
