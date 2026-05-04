"""Config flow for DMI Observation."""

from __future__ import annotations

import logging
import math
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.selector import NumberSelector, NumberSelectorConfig, NumberSelectorMode, SelectOptionDict, SelectSelector, SelectSelectorConfig, SelectSelectorMode

from .api import (
    DMIConnectionError,
    DMIObservationClient,
    StationOption,
    DMIStationNotFoundError,
)
from .const import (
    CONF_SCAN_INTERVAL,
    CONF_STATION_ID,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


def _user_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Return the schema for the main config form."""
    defaults = defaults or {}

    return vol.Schema(
        {
            vol.Required(
                CONF_STATION_ID,
                default=defaults.get(CONF_STATION_ID, ""),
            ): str,
        }
    )


def _location_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Return the schema for latitude/longitude input."""
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_LATITUDE,
                default=defaults.get(CONF_LATITUDE),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=-90,
                    max=90,
                    step="any",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_LONGITUDE,
                default=defaults.get(CONF_LONGITUDE),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=-180,
                    max=180,
                    step="any",
                    mode=NumberSelectorMode.BOX,
                )
            ),
        }
    )


def _station_schema(
    options: list[SelectOptionDict],
    default: str | None = None,
) -> vol.Schema:
    """Return the schema for station selection."""
    return vol.Schema(
        {
            vol.Required(
                CONF_STATION_ID,
                default=default if default is not None else (options[0]["value"] if options else ""),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=options,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )
        }
    )


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input."""
    client = DMIObservationClient(aiohttp_client.async_get_clientsession(hass))

    snapshot = await client.async_get_snapshot(data[CONF_STATION_ID])
    station = snapshot.station

    return {
        "title": f"{station.name} ({station.station_id})",
        "station_id": station.station_id,
    }


class DMIObservationConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the config flow for DMI Observation."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._station_options: list[StationOption] = []
        self._search_location: dict[str, float] = {}

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the options flow handler."""
        return DMIObservationOptionsFlow(config_entry)

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        """Handle the location input step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                lat = float(user_input[CONF_LATITUDE])
                lon = float(user_input[CONF_LONGITUDE])
            except (TypeError, ValueError):
                errors["base"] = "invalid_location"
            else:
                self._search_location = {
                    CONF_LATITUDE: lat,
                    CONF_LONGITUDE: lon,
                }
                return await self.async_step_station_select()

        defaults = self._search_location or {
            CONF_LATITUDE: self.hass.config.latitude,
            CONF_LONGITUDE: self.hass.config.longitude,
        }

        return self.async_show_form(
            step_id="user",
            data_schema=_location_schema(defaults),
            errors=errors,
        )

    async def async_step_station_select(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        """Handle the distance-sorted station selection step."""
        errors: dict[str, str] = {}

        if not self._station_options:
            try:
                stations = await self._async_fetch_station_options()
                self._station_options = self._sort_stations_by_distance(
                    stations,
                    self._search_location[CONF_LATITUDE],
                    self._search_location[CONF_LONGITUDE],
                )
            except DMIConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected exception during station list fetch")
                errors["base"] = "unknown"

        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
            except DMIStationNotFoundError:
                errors["base"] = "station_not_found"
            except DMIConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected exception during DMI validation")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(info["station_id"])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=info["title"],
                    data={
                        **user_input,
                        **self._search_location,
                    },
                )

        return self.async_show_form(
            step_id="station_select",
            data_schema=self._build_station_form(),
            errors=errors,
            description_placeholders={
                "latitude": str(self._search_location.get(CONF_LATITUDE, "")),
                "longitude": str(self._search_location.get(CONF_LONGITUDE, "")),
            },
        )

    async def async_step_reconfigure(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        """Handle integration reconfiguration."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()

        if not self._station_options:
            try:
                stations = await self._async_fetch_station_options()
                self._search_location = {
                    CONF_LATITUDE: float(entry.data.get(CONF_LATITUDE, self.hass.config.latitude)),
                    CONF_LONGITUDE: float(entry.data.get(CONF_LONGITUDE, self.hass.config.longitude)),
                }
                self._station_options = self._sort_stations_by_distance(
                    stations,
                    self._search_location[CONF_LATITUDE],
                    self._search_location[CONF_LONGITUDE],
                )
            except DMIConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected exception during station list fetch")
                errors["base"] = "unknown"

        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
            except DMIStationNotFoundError:
                errors["base"] = "station_not_found"
            except DMIConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected exception during DMI reconfigure")
                errors["base"] = "unknown"
            else:
                for other_entry in self._async_current_entries():
                    if (
                        other_entry.entry_id != entry.entry_id
                        and other_entry.unique_id == info["station_id"]
                    ):
                        return self.async_abort(reason="already_configured")

                self.hass.config_entries.async_update_entry(
                    entry,
                    data={
                        **user_input,
                        **self._search_location,
                    },
                    title=info["title"],
                    unique_id=info["station_id"],
                )
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reconfigure_successful")

        defaults = {
            CONF_STATION_ID: entry.data.get(CONF_STATION_ID, ""),
        }

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self._build_station_form(defaults.get(CONF_STATION_ID)),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """Handle re-authentication."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        """Handle re-authentication confirmation."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if not self._station_options:
            try:
                stations = await self._async_fetch_station_options()
                self._search_location = {
                    CONF_LATITUDE: float(entry.data.get(CONF_LATITUDE, self.hass.config.latitude)),
                    CONF_LONGITUDE: float(entry.data.get(CONF_LONGITUDE, self.hass.config.longitude)),
                }
                self._station_options = self._sort_stations_by_distance(
                    stations,
                    self._search_location[CONF_LATITUDE],
                    self._search_location[CONF_LONGITUDE],
                )
            except DMIConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected exception during station list fetch")
                errors["base"] = "unknown"

        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
            except DMIStationNotFoundError:
                errors["base"] = "station_not_found"
            except DMIConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected exception during DMI reauth")
                errors["base"] = "unknown"
            else:
                for other_entry in self._async_current_entries():
                    if (
                        other_entry.entry_id != entry.entry_id
                        and other_entry.unique_id == info["station_id"]
                    ):
                        return self.async_abort(reason="already_configured")

                self.hass.config_entries.async_update_entry(
                    entry,
                    data={
                        **user_input,
                        **self._search_location,
                    },
                    title=info["title"],
                    unique_id=info["station_id"],
                )
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        defaults = {
            CONF_STATION_ID: entry.data.get(CONF_STATION_ID, ""),
        }

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self._build_station_form(defaults.get(CONF_STATION_ID)),
            errors=errors,
        )

    async def _async_fetch_station_options(self) -> list[StationOption]:
        """Load active stations from DMI."""
        client = DMIObservationClient(aiohttp_client.async_get_clientsession(self.hass))
        return await client.async_get_active_stations()

    def _build_station_form(self, default_station_id: str | None = None) -> vol.Schema:
        """Build the station dropdown form."""
        options = [
            SelectOptionDict(
                value=option.station_id,
                label=self._format_station_label(option),
            )
            for option in self._station_options
        ]

        if options:
            return _station_schema(options, default_station_id)

        return _user_schema(
            {CONF_STATION_ID: default_station_id or ""}
        )

    @staticmethod
    def _format_station_label(option: StationOption) -> str:
        """Format a station label for the dropdown."""
        details = [detail for detail in (option.type, option.country) if detail]
        distance = (
            f"{option.distance_km:.1f} km"
            if option.distance_km is not None
            else None
        )
        if distance:
            details.insert(0, distance)
        if details:
            return f"{option.name} ({option.station_id}, {', '.join(details)})"
        return f"{option.name} ({option.station_id})"

    @staticmethod
    def _sort_stations_by_distance(
        stations: list[StationOption],
        latitude: float,
        longitude: float,
    ) -> list[StationOption]:
        """Sort stations by distance to the provided coordinates."""
        sorted_stations: list[StationOption] = []

        for station in stations:
            distance_km = None
            if station.latitude is not None and station.longitude is not None:
                distance_km = _haversine_km(
                    latitude,
                    longitude,
                    station.latitude,
                    station.longitude,
                )
            sorted_stations.append(
                StationOption(
                    station_id=station.station_id,
                    name=station.name,
                    type=station.type,
                    country=station.country,
                    latitude=station.latitude,
                    longitude=station.longitude,
                    distance_km=distance_km,
                )
            )

        return sorted(
            sorted_stations,
            key=lambda station: (
                station.distance_km is None,
                station.distance_km if station.distance_km is not None else float("inf"),
                station.name.casefold(),
                station.station_id,
            ),
        )


def _haversine_km(
    latitude_1: float,
    longitude_1: float,
    latitude_2: float,
    longitude_2: float,
) -> float:
    """Calculate the great-circle distance between two coordinates."""
    radius_km = 6371.0
    lat1 = math.radians(latitude_1)
    lon1 = math.radians(longitude_1)
    lat2 = math.radians(latitude_2)
    lon2 = math.radians(longitude_2)

    delta_lat = lat2 - lat1
    delta_lon = lon2 - lon1
    a = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return radius_km * c


class DMIObservationOptionsFlow(config_entries.OptionsFlow):
    """Handle options for DMI Observation."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize the options flow."""
        self.config_entry = config_entry

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_interval = self.config_entry.options.get(
            CONF_SCAN_INTERVAL,
            self.config_entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=current_interval,
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL),
                    )
                }
            ),
        )
