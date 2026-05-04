"""Constants for the DMI Observation integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "dmi_observation"
PLATFORMS: list[Platform] = [Platform.SENSOR]

CONF_SCAN_INTERVAL = "scan_interval"
CONF_STATION_ID = "station_id"

DEFAULT_SCAN_INTERVAL = 600
MIN_SCAN_INTERVAL = 600
MAX_SCAN_INTERVAL = 3600

ATTRIBUTION = "Data provided by DMI Open Data"

PERIOD_LATEST_10_MINUTES = "latest-10-minutes"
PERIOD_LATEST_HOUR = "latest-hour"

DEFAULT_NAME = "DMI Observation"
