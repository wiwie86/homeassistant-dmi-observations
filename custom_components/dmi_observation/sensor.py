"""Sensor platform for DMI Observation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription
from homeassistant.components.sensor.const import SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, PERCENTAGE, UnitOfLength, UnitOfPressure, UnitOfSpeed, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import ObservationSnapshot, ObservationValue
from .const import ATTRIBUTION, DOMAIN, PERIOD_LATEST_10_MINUTES, PERIOD_LATEST_HOUR
from .coordinator import DMIObservationCoordinator

_DEVICE_INFO_CACHE: dict[str, DeviceInfo] = {}


@dataclass(frozen=True, kw_only=True)
class DMIObservationSensorDescription(SensorEntityDescription):
    """Describe a DMI observation sensor."""

    value_fn: Callable[[ObservationSnapshot], Any]
    attributes_fn: Callable[[ObservationSnapshot], dict[str, Any]]


def _get_observation(snapshot: ObservationSnapshot, parameter_id: str) -> ObservationValue | None:
    """Return the parsed observation for a parameter."""
    return snapshot.parameters.get(parameter_id)


def _coerce_float(value: Any) -> float | None:
    """Convert a value into a float when possible."""
    if value is None:
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _numeric_value(snapshot: ObservationSnapshot, parameter_id: str) -> float | None:
    """Return a numeric parameter value."""
    observation = _get_observation(snapshot, parameter_id)
    if observation is None:
        return None
    return _coerce_float(observation.value)


def _common_attributes(snapshot: ObservationSnapshot) -> dict[str, Any]:
    """Return attributes shared by all entities."""
    return {
        "attribution": ATTRIBUTION,
        "station_id": snapshot.station.station_id,
        "station_name": snapshot.station.name,
    }


def _observation_attributes(snapshot: ObservationSnapshot, parameter_id: str) -> dict[str, Any]:
    """Return attributes for a specific parameter."""
    attributes = _common_attributes(snapshot)
    observation = _get_observation(snapshot, parameter_id)
    if observation is None:
        return attributes

    attributes.update(
        {
            "parameter_id": observation.parameter_id,
            "observed": observation.observed.isoformat(),
            "source_period": observation.period,
        }
    )

    if observation.created is not None:
        attributes["created"] = observation.created.isoformat()

    return attributes


def _weather_value(snapshot: ObservationSnapshot) -> str | None:
    """Return a readable weather description if present."""
    observation = _get_observation(snapshot, "weather")
    if observation is None:
        return None

    numeric = _coerce_float(observation.value)
    if numeric is None:
        return str(observation.value)

    return _describe_weather_code(int(numeric))


def _weather_attributes(snapshot: ObservationSnapshot) -> dict[str, Any]:
    """Return weather-specific attributes."""
    attributes = _observation_attributes(snapshot, "weather")
    observation = _get_observation(snapshot, "weather")
    if observation is not None:
        attributes["weather_code"] = observation.value
    return attributes


def _wind_direction_value(snapshot: ObservationSnapshot, parameter_id: str) -> str | None:
    """Return a compass direction string."""
    observation = _get_observation(snapshot, parameter_id)
    if observation is None:
        return None

    degrees = _coerce_float(observation.value)
    if degrees is None:
        return str(observation.value)

    return _degrees_to_compass(degrees)


def _wind_direction_attributes(snapshot: ObservationSnapshot, parameter_id: str) -> dict[str, Any]:
    """Return wind direction attributes."""
    attributes = _observation_attributes(snapshot, parameter_id)
    observation = _get_observation(snapshot, parameter_id)
    if observation is not None:
        attributes["degrees"] = observation.value
    return attributes


def _api_last_update(snapshot: ObservationSnapshot) -> datetime | None:
    """Return the DMI API timestamp."""
    return snapshot.api_timestamp


def _latest_observed(snapshot: ObservationSnapshot) -> datetime | None:
    """Return the newest observation timestamp."""
    return snapshot.latest_observed


def _latest_observed_attributes(snapshot: ObservationSnapshot) -> dict[str, Any]:
    """Return diagnostic attributes about the fetched dataset."""
    attributes = _common_attributes(snapshot)
    attributes.update(
        {
            "fetched_at": snapshot.fetched_at.isoformat(),
            PERIOD_LATEST_10_MINUTES: snapshot.source_counts.get(PERIOD_LATEST_10_MINUTES, 0),
            PERIOD_LATEST_HOUR: snapshot.source_counts.get(PERIOD_LATEST_HOUR, 0),
            "available_parameters": sorted(snapshot.parameters.keys()),
            "supported_parameters": list(snapshot.station.supported_parameters),
        }
    )
    return attributes


def _precipitation_history_attributes(
    snapshot: ObservationSnapshot,
    parameter_id: str,
) -> dict[str, Any]:
    """Return precipitation history attributes."""
    attributes = _observation_attributes(snapshot, parameter_id)
    history = snapshot.history.get(parameter_id, ())
    if history:
        attributes["history"] = [
            {
                "observed": observation.observed.isoformat(),
                "value": observation.value,
            }
            for observation in history[-18:]
        ]
    return attributes


def _sum_precipitation_history(
    snapshot: ObservationSnapshot,
    parameter_id: str,
    hours: int,
) -> float | None:
    """Sum precipitation history over the requested number of hours."""
    history = snapshot.history.get(parameter_id, ())
    if not history:
        return None

    latest_observed = history[-1].observed
    cutoff = latest_observed.timestamp() - hours * 3600

    relevant = [
        _coerce_float(observation.value)
        for observation in history
        if observation.observed.timestamp() > cutoff
    ]
    values = [value for value in relevant if value is not None]

    if not values:
        return None

    return round(sum(values), 2)


def _derived_precipitation_attributes(
    snapshot: ObservationSnapshot,
    hours: int,
) -> dict[str, Any]:
    """Return attributes for a derived precipitation aggregate."""
    attributes = _common_attributes(snapshot)
    history = snapshot.history.get("precip_past10min", ())
    if history:
        latest = history[-1]
        cutoff = latest.observed.timestamp() - hours * 3600
        relevant = [
            observation
            for observation in history
            if observation.observed.timestamp() > cutoff
        ]
        attributes.update(
            {
                "derived_from": "precip_past10min",
                "window": f"{hours}h",
                "latest_sample": latest.observed.isoformat(),
                "sample_count": len(relevant),
            }
        )
        attributes["history"] = [
            {
                "observed": observation.observed.isoformat(),
                "value": observation.value,
            }
            for observation in relevant
        ]
    return attributes


def _precipitation_24h_value(snapshot: ObservationSnapshot) -> float | None:
    """Return precipitation over the past 24 hours."""
    value = _numeric_value(snapshot, "precip_past24h")
    if value is not None:
        return value
    return _sum_precipitation_history(snapshot, "precip_past10min", 24)


def _precipitation_24h_attributes(snapshot: ObservationSnapshot) -> dict[str, Any]:
    """Return attributes for the 24-hour precipitation sensor."""
    if _numeric_value(snapshot, "precip_past24h") is not None:
        attributes = _precipitation_history_attributes(snapshot, "precip_past24h")
        attributes["derived_from"] = "precip_past24h"
        attributes["window"] = "24h"
        return attributes

    attributes = _derived_precipitation_attributes(snapshot, 24)
    attributes["note"] = "No native precip_past24h sample available; using summed precip_past10min history."
    return attributes


def _precipitation_now_attributes(snapshot: ObservationSnapshot) -> dict[str, Any]:
    """Return attributes for current precipitation."""
    history = snapshot.history.get("precip_past1min", ())
    if history:
        return _precipitation_history_attributes(snapshot, "precip_past1min")

    attributes = _precipitation_history_attributes(snapshot, "precip_past10min")
    attributes["derived_from"] = "precip_past10min"
    attributes["note"] = "No precip_past1min sample available; using the latest 10-minute observation."
    return attributes


def _precipitation_now_value(snapshot: ObservationSnapshot) -> float | None:
    """Return the best available current precipitation value."""
    value = _numeric_value(snapshot, "precip_past1min")
    if value is not None:
        return value
    return _numeric_value(snapshot, "precip_past10min")


SENSOR_DESCRIPTIONS: tuple[DMIObservationSensorDescription, ...] = (
    DMIObservationSensorDescription(
        key="precipitation_now",
        name="Precipitation Now",
        icon="mdi:weather-rainy",
        native_unit_of_measurement=UnitOfLength.MILLIMETERS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=_precipitation_now_value,
        attributes_fn=_precipitation_now_attributes,
    ),
    DMIObservationSensorDescription(
        key="precipitation_past_10_minutes",
        name="Precipitation Past 10 Minutes",
        icon="mdi:weather-rainy",
        native_unit_of_measurement=UnitOfLength.MILLIMETERS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda snapshot: _numeric_value(snapshot, "precip_past10min"),
        attributes_fn=lambda snapshot: _precipitation_history_attributes(snapshot, "precip_past10min"),
    ),
    DMIObservationSensorDescription(
        key="precipitation_past_1_minute",
        name="Precipitation Past 1 Minute",
        icon="mdi:weather-rainy",
        native_unit_of_measurement=UnitOfLength.MILLIMETERS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
        value_fn=lambda snapshot: _numeric_value(snapshot, "precip_past1min"),
        attributes_fn=lambda snapshot: _precipitation_history_attributes(snapshot, "precip_past1min"),
    ),
    DMIObservationSensorDescription(
        key="precipitation_past_hour",
        name="Precipitation Past Hour",
        icon="mdi:weather-pouring",
        native_unit_of_measurement=UnitOfLength.MILLIMETERS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda snapshot: _numeric_value(snapshot, "precip_past1h"),
        attributes_fn=lambda snapshot: _precipitation_history_attributes(snapshot, "precip_past1h"),
    ),
    DMIObservationSensorDescription(
        key="precipitation_past_3_hours",
        name="Precipitation Past 3 Hours",
        icon="mdi:weather-pouring",
        native_unit_of_measurement=UnitOfLength.MILLIMETERS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda snapshot: _sum_precipitation_history(snapshot, "precip_past10min", 3),
        attributes_fn=lambda snapshot: _derived_precipitation_attributes(snapshot, 3),
    ),
    DMIObservationSensorDescription(
        key="precipitation_past_6_hours",
        name="Precipitation Past 6 Hours",
        icon="mdi:weather-pouring",
        native_unit_of_measurement=UnitOfLength.MILLIMETERS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda snapshot: _sum_precipitation_history(snapshot, "precip_past10min", 6),
        attributes_fn=lambda snapshot: _derived_precipitation_attributes(snapshot, 6),
    ),
    DMIObservationSensorDescription(
        key="precipitation_past_12_hours",
        name="Precipitation Past 12 Hours",
        icon="mdi:weather-pouring",
        native_unit_of_measurement=UnitOfLength.MILLIMETERS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda snapshot: _sum_precipitation_history(snapshot, "precip_past10min", 12),
        attributes_fn=lambda snapshot: _derived_precipitation_attributes(snapshot, 12),
    ),
    DMIObservationSensorDescription(
        key="precipitation_past_24_hours",
        name="Precipitation Past 24 Hours",
        icon="mdi:weather-pouring",
        native_unit_of_measurement=UnitOfLength.MILLIMETERS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=_precipitation_24h_value,
        attributes_fn=_precipitation_24h_attributes,
    ),
    DMIObservationSensorDescription(
        key="precipitation_duration_past_10_minutes",
        name="Precipitation Duration Past 10 Minutes",
        icon="mdi:timer-outline",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_registry_enabled_default=False,
        value_fn=lambda snapshot: _numeric_value(snapshot, "precip_dur_past10min"),
        attributes_fn=lambda snapshot: _observation_attributes(snapshot, "precip_dur_past10min"),
    ),
    DMIObservationSensorDescription(
        key="precipitation_duration_past_hour",
        name="Precipitation Duration Past Hour",
        icon="mdi:timer-outline",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_registry_enabled_default=False,
        value_fn=lambda snapshot: _numeric_value(snapshot, "precip_dur_past1h"),
        attributes_fn=lambda snapshot: _observation_attributes(snapshot, "precip_dur_past1h"),
    ),
    DMIObservationSensorDescription(
        key="temperature",
        name="Temperature",
        icon="mdi:thermometer",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda snapshot: _numeric_value(snapshot, "temp_dry"),
        attributes_fn=lambda snapshot: _observation_attributes(snapshot, "temp_dry"),
    ),
    DMIObservationSensorDescription(
        key="dew_point",
        name="Dew Point",
        icon="mdi:thermometer-water",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda snapshot: _numeric_value(snapshot, "temp_dew"),
        attributes_fn=lambda snapshot: _observation_attributes(snapshot, "temp_dew"),
    ),
    DMIObservationSensorDescription(
        key="humidity",
        name="Humidity",
        icon="mdi:water-percent",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda snapshot: _numeric_value(snapshot, "humidity"),
        attributes_fn=lambda snapshot: _observation_attributes(snapshot, "humidity"),
    ),
    DMIObservationSensorDescription(
        key="humidity_past_hour",
        name="Humidity Past Hour",
        icon="mdi:water-percent",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_registry_enabled_default=False,
        value_fn=lambda snapshot: _numeric_value(snapshot, "humidity_past1h"),
        attributes_fn=lambda snapshot: _observation_attributes(snapshot, "humidity_past1h"),
    ),
    DMIObservationSensorDescription(
        key="pressure",
        name="Pressure",
        icon="mdi:gauge",
        device_class=SensorDeviceClass.PRESSURE,
        native_unit_of_measurement=UnitOfPressure.HPA,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda snapshot: _numeric_value(snapshot, "pressure"),
        attributes_fn=lambda snapshot: _observation_attributes(snapshot, "pressure"),
    ),
    DMIObservationSensorDescription(
        key="pressure_at_sea",
        name="Pressure At Sea Level",
        icon="mdi:gauge",
        device_class=SensorDeviceClass.PRESSURE,
        native_unit_of_measurement=UnitOfPressure.HPA,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda snapshot: _numeric_value(snapshot, "pressure_at_sea"),
        attributes_fn=lambda snapshot: _observation_attributes(snapshot, "pressure_at_sea"),
    ),
    DMIObservationSensorDescription(
        key="cloud_cover",
        name="Cloud Cover",
        icon="mdi:weather-cloudy",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda snapshot: _numeric_value(snapshot, "cloud_cover"),
        attributes_fn=lambda snapshot: _observation_attributes(snapshot, "cloud_cover"),
    ),
    DMIObservationSensorDescription(
        key="cloud_height",
        name="Cloud Height",
        icon="mdi:cloud",
        native_unit_of_measurement=UnitOfLength.METERS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda snapshot: _numeric_value(snapshot, "cloud_height"),
        attributes_fn=lambda snapshot: _observation_attributes(snapshot, "cloud_height"),
    ),
    DMIObservationSensorDescription(
        key="visibility",
        name="Visibility",
        icon="mdi:eye",
        native_unit_of_measurement=UnitOfLength.METERS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda snapshot: _numeric_value(snapshot, "visibility"),
        attributes_fn=lambda snapshot: _observation_attributes(snapshot, "visibility"),
    ),
    DMIObservationSensorDescription(
        key="visibility_mean_last_10_minutes",
        name="Visibility Mean Last 10 Minutes",
        icon="mdi:eye",
        native_unit_of_measurement=UnitOfLength.METERS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_registry_enabled_default=False,
        value_fn=lambda snapshot: _numeric_value(snapshot, "visib_mean_last10min"),
        attributes_fn=lambda snapshot: _observation_attributes(snapshot, "visib_mean_last10min"),
    ),
    DMIObservationSensorDescription(
        key="weather",
        name="Weather",
        icon="mdi:weather-partly-cloudy",
        value_fn=_weather_value,
        attributes_fn=_weather_attributes,
    ),
    DMIObservationSensorDescription(
        key="wind_direction",
        name="Wind Direction",
        icon="mdi:compass-outline",
        value_fn=lambda snapshot: _wind_direction_value(snapshot, "wind_dir"),
        attributes_fn=lambda snapshot: _wind_direction_attributes(snapshot, "wind_dir"),
    ),
    DMIObservationSensorDescription(
        key="wind_direction_past_hour",
        name="Wind Direction Past Hour",
        icon="mdi:compass-outline",
        entity_registry_enabled_default=False,
        value_fn=lambda snapshot: _wind_direction_value(snapshot, "wind_dir_past1h"),
        attributes_fn=lambda snapshot: _wind_direction_attributes(snapshot, "wind_dir_past1h"),
    ),
    DMIObservationSensorDescription(
        key="wind_speed",
        name="Wind Speed",
        icon="mdi:weather-windy",
        native_unit_of_measurement=UnitOfSpeed.METERS_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda snapshot: _numeric_value(snapshot, "wind_speed"),
        attributes_fn=lambda snapshot: _observation_attributes(snapshot, "wind_speed"),
    ),
    DMIObservationSensorDescription(
        key="wind_speed_past_hour",
        name="Wind Speed Past Hour",
        icon="mdi:weather-windy",
        native_unit_of_measurement=UnitOfSpeed.METERS_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
        value_fn=lambda snapshot: _numeric_value(snapshot, "wind_speed_past1h"),
        attributes_fn=lambda snapshot: _observation_attributes(snapshot, "wind_speed_past1h"),
    ),
    DMIObservationSensorDescription(
        key="wind_max",
        name="Wind Max",
        icon="mdi:weather-windy",
        native_unit_of_measurement=UnitOfSpeed.METERS_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda snapshot: _numeric_value(snapshot, "wind_max"),
        attributes_fn=lambda snapshot: _observation_attributes(snapshot, "wind_max"),
    ),
    DMIObservationSensorDescription(
        key="wind_min",
        name="Wind Min",
        icon="mdi:weather-windy",
        native_unit_of_measurement=UnitOfSpeed.METERS_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
        value_fn=lambda snapshot: _numeric_value(snapshot, "wind_min"),
        attributes_fn=lambda snapshot: _observation_attributes(snapshot, "wind_min"),
    ),
    DMIObservationSensorDescription(
        key="wind_gust_past_hour",
        name="Wind Gust Past Hour",
        icon="mdi:weather-windy",
        native_unit_of_measurement=UnitOfSpeed.METERS_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
        value_fn=lambda snapshot: _numeric_value(snapshot, "wind_gust_always_past1h"),
        attributes_fn=lambda snapshot: _observation_attributes(snapshot, "wind_gust_always_past1h"),
    ),
    DMIObservationSensorDescription(
        key="temperature_max_past_hour",
        name="Temperature Max Past Hour",
        icon="mdi:thermometer-chevron-up",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
        value_fn=lambda snapshot: _numeric_value(snapshot, "temp_max_past1h"),
        attributes_fn=lambda snapshot: _observation_attributes(snapshot, "temp_max_past1h"),
    ),
    DMIObservationSensorDescription(
        key="temperature_mean_past_hour",
        name="Temperature Mean Past Hour",
        icon="mdi:thermometer-lines",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
        value_fn=lambda snapshot: _numeric_value(snapshot, "temp_mean_past1h"),
        attributes_fn=lambda snapshot: _observation_attributes(snapshot, "temp_mean_past1h"),
    ),
    DMIObservationSensorDescription(
        key="temperature_min_past_hour",
        name="Temperature Min Past Hour",
        icon="mdi:thermometer-chevron-down",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
        value_fn=lambda snapshot: _numeric_value(snapshot, "temp_min_past1h"),
        attributes_fn=lambda snapshot: _observation_attributes(snapshot, "temp_min_past1h"),
    ),
    DMIObservationSensorDescription(
        key="latest_observed",
        name="Latest Observed",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_latest_observed,
        attributes_fn=_latest_observed_attributes,
    ),
    DMIObservationSensorDescription(
        key="api_last_update",
        name="API Last Update",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_api_last_update,
        attributes_fn=_latest_observed_attributes,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up DMI Observation sensors from a config entry."""
    coordinator: DMIObservationCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        DMIObservationSensor(coordinator, description) for description in SENSOR_DESCRIPTIONS
    )


class DMIObservationSensor(
    CoordinatorEntity[DMIObservationCoordinator],
    SensorEntity,
):
    """Representation of one DMI observation sensor."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DMIObservationCoordinator,
        description: DMIObservationSensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = (
            f"{DOMAIN}_{coordinator.station_id}_{description.key}"
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info for the station."""
        cache_key = self.coordinator.station_id
        station = self.coordinator.data.station

        if cache_key not in _DEVICE_INFO_CACHE:
            _DEVICE_INFO_CACHE[cache_key] = DeviceInfo(
                identifiers={(DOMAIN, self.coordinator.station_id)},
                manufacturer="DMI",
                model="Observation Station",
                name=station.name,
                suggested_area=station.name,
                configuration_url="https://www.dmi.dk/friedata/observationer",
            )

        return _DEVICE_INFO_CACHE[cache_key]

    @property
    def native_value(self) -> Any:
        """Return the current sensor value."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        return self.entity_description.attributes_fn(self.coordinator.data)


def _degrees_to_compass(degrees: float) -> str:
    """Convert degrees to a 16-point compass direction."""
    directions = (
        "N",
        "NNE",
        "NE",
        "ENE",
        "E",
        "ESE",
        "SE",
        "SSE",
        "S",
        "SSW",
        "SW",
        "WSW",
        "W",
        "WNW",
        "NW",
        "NNW",
    )
    index = round(degrees / 22.5) % len(directions)
    return directions[index]


def _describe_weather_code(code: int) -> str:
    """Return a readable description for a DMI weather code."""
    known = {
        100: "No significant weather observed",
        101: "Clouds dissolving or becoming less developed",
        102: "Sky state unchanged",
        103: "Clouds forming or developing",
        104: "Haze or smoke, visibility at least 1 km",
        105: "Haze or smoke, visibility below 1 km",
        110: "Fog",
        111: "Diamond dust",
        112: "Distant lightning",
        118: "Squalls",
        121: "Precipitation",
        122: "Drizzle or snow grains",
        123: "Rain",
        124: "Snow",
        125: "Freezing rain",
        126: "Thunderstorm",
        127: "Blowing or drifting snow or sand",
        130: "Fog",
        131: "Fog or ice fog in patches",
        132: "Fog or ice fog becoming thinner",
        133: "Fog or ice fog with little change",
        134: "Fog or ice fog forming or becoming thicker",
        135: "Fog depositing rime",
        141: "Precipitation, slight or moderate",
        142: "Precipitation, heavy",
        143: "Liquid precipitation, slight or moderate",
        144: "Liquid precipitation, heavy",
        145: "Solid precipitation, slight or moderate",
        146: "Solid precipitation, heavy",
        147: "Freezing precipitation, slight or moderate",
        148: "Freezing precipitation, heavy",
        150: "Drizzle",
        151: "Drizzle, slight",
        152: "Drizzle, moderate",
        153: "Drizzle, heavy",
        154: "Freezing drizzle, slight",
        155: "Freezing drizzle, moderate",
        156: "Freezing drizzle, heavy",
        157: "Drizzle and rain, slight",
        158: "Drizzle and rain, moderate or heavy",
        160: "Rain",
        161: "Rain, slight",
        162: "Rain, moderate",
        163: "Rain, heavy",
        164: "Freezing rain, slight",
        165: "Freezing rain, moderate",
        166: "Freezing rain, heavy",
        167: "Rain or drizzle mixed with snow, slight",
        168: "Rain or drizzle mixed with snow, moderate or heavy",
        170: "Snow",
        171: "Snow, slight",
        172: "Snow, moderate",
        173: "Snow, heavy",
        174: "Ice pellets, slight",
        175: "Ice pellets, moderate",
        176: "Ice pellets, heavy",
        177: "Snow grains",
        178: "Ice crystals",
        180: "Showers or intermittent precipitation",
        181: "Rain shower, slight",
        182: "Rain shower, moderate",
        183: "Rain shower, heavy",
        184: "Violent rain shower",
        185: "Snow shower, slight",
        186: "Snow shower, moderate",
        187: "Snow shower, heavy",
        189: "Hail",
        190: "Thunderstorm",
        191: "Thunderstorm, slight or moderate, without precipitation",
        192: "Thunderstorm, slight or moderate, with rain or snow showers",
        193: "Thunderstorm, slight or moderate, with hail",
        194: "Thunderstorm, heavy, without precipitation",
        195: "Thunderstorm, heavy, with rain or snow showers",
        196: "Thunderstorm, heavy, with hail",
        199: "Tornado",
    }
    return known.get(code, f"Weather code {code}")
