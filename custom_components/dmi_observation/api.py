"""API client for DMI Open Data observations."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from aiohttp import ClientError, ClientResponseError, ClientSession, ClientTimeout

from .const import PERIOD_LATEST_10_MINUTES, PERIOD_LATEST_HOUR

BASE_URL = "https://opendataapi.dmi.dk/v2/metObs/collections"
REQUEST_TIMEOUT = ClientTimeout(total=30)
PRECIPITATION_HISTORY_LOOKBACK = timedelta(hours=25)


class DMIObservationError(Exception):
    """Base exception for DMI observation errors."""


class DMIConnectionError(DMIObservationError):
    """Raised when the API cannot be reached."""


class DMIStationNotFoundError(DMIObservationError):
    """Raised when the station cannot be found."""


@dataclass(slots=True)
class StationDetails:
    """Station metadata from DMI."""

    station_id: str
    name: str
    owner: str | None
    status: str | None
    region_id: str | None
    latitude: float | None
    longitude: float | None
    altitude: float | None
    supported_parameters: tuple[str, ...]


@dataclass(slots=True)
class StationOption:
    """Station option for config flow selection."""

    station_id: str
    name: str
    type: str | None
    country: str | None
    latitude: float | None
    longitude: float | None
    distance_km: float | None = None


@dataclass(slots=True)
class ObservationValue:
    """Single parsed observation value."""

    parameter_id: str
    value: Any
    observed: datetime
    created: datetime | None
    period: str


@dataclass(slots=True)
class ObservationSnapshot:
    """Combined observation snapshot for one station."""

    station: StationDetails
    parameters: dict[str, ObservationValue]
    history: dict[str, tuple[ObservationValue, ...]]
    fetched_at: datetime
    api_timestamp: datetime | None
    latest_observed: datetime | None
    source_counts: dict[str, int]


class DMIObservationClient:
    """Small async client for the DMI observation endpoints."""

    def __init__(self, session: ClientSession) -> None:
        """Initialize the client."""
        self._session = session
        self._station_cache: dict[str, StationDetails] = {}

    async def async_get_active_stations(self) -> list[StationOption]:
        """Fetch active stations for the config flow dropdown."""
        now = _format_datetime(datetime.now(tz=UTC).replace(microsecond=0))
        payload = await self._async_get_json(
            "station/items",
            {
                "status": "Active",
                "datetime": f"{now}/..",
                "limit": "1000",
            },
        )

        options = [
            StationOption(
                station_id=str(properties.get("stationId")),
                name=properties.get("name") or f"Station {properties.get('stationId')}",
                type=properties.get("type"),
                country=properties.get("country"),
                latitude=coordinates[1] if len(coordinates) > 1 else None,
                longitude=coordinates[0] if len(coordinates) > 0 else None,
            )
            for feature in payload.get("features", [])
            if (properties := feature.get("properties", {})).get("stationId")
            if (coordinates := feature.get("geometry", {}).get("coordinates", [])) is not None
        ]

        return sorted(options, key=lambda option: (option.name.casefold(), option.station_id))

    async def async_get_station(self, station_id: str) -> StationDetails:
        """Fetch station metadata."""
        if station_id in self._station_cache:
            return self._station_cache[station_id]

        payload = await self._async_get_json(
            "station/items",
            {"stationId": station_id},
        )

        features = payload.get("features", [])
        if not features:
            raise DMIStationNotFoundError(f"Station {station_id} was not found")

        feature = self._select_station_feature(features)
        properties = feature.get("properties", {})
        coordinates = feature.get("geometry", {}).get("coordinates", [None, None])

        station = StationDetails(
            station_id=str(properties.get("stationId", station_id)),
            name=properties.get("name") or f"DMI Station {station_id}",
            owner=properties.get("owner"),
            status=properties.get("status"),
            region_id=properties.get("regionId"),
            latitude=coordinates[1] if len(coordinates) > 1 else None,
            longitude=coordinates[0] if len(coordinates) > 0 else None,
            altitude=properties.get("stationHeight"),
            supported_parameters=tuple(sorted(properties.get("parameterId", []))),
        )

        self._station_cache[station_id] = station
        return station

    async def async_get_snapshot(self, station_id: str) -> ObservationSnapshot:
        """Fetch station metadata and the latest observation snapshot."""
        now = datetime.now(tz=UTC).replace(microsecond=0)
        history_start = _format_datetime(now - PRECIPITATION_HISTORY_LOOKBACK)
        history_end = _format_datetime(now)

        (
            station,
            latest_10_minutes,
            latest_hour,
            precip_1_minute_history,
            precip_10_minutes_history,
            precip_1_hour_history,
            precip_24_hours_history,
        ) = await asyncio.gather(
            self.async_get_station(station_id),
            self._async_get_json(
                "observation/items",
                {"stationId": station_id, "period": PERIOD_LATEST_10_MINUTES},
            ),
            self._async_get_json(
                "observation/items",
                {"stationId": station_id, "period": PERIOD_LATEST_HOUR},
            ),
            self._async_get_json(
                "observation/items",
                {
                    "stationId": station_id,
                    "parameterId": "precip_past1min",
                    "datetime": f"{history_start}/{history_end}",
                },
            ),
            self._async_get_json(
                "observation/items",
                {
                    "stationId": station_id,
                    "parameterId": "precip_past10min",
                    "datetime": f"{history_start}/{history_end}",
                },
            ),
            self._async_get_json(
                "observation/items",
                {
                    "stationId": station_id,
                    "parameterId": "precip_past1h",
                    "datetime": f"{history_start}/{history_end}",
                },
            ),
            self._async_get_json(
                "observation/items",
                {
                    "stationId": station_id,
                    "parameterId": "precip_past24h",
                    "datetime": f"{history_start}/{history_end}",
                },
            ),
        )

        parameters: dict[str, ObservationValue] = {}

        for period, payload in (
            (PERIOD_LATEST_10_MINUTES, latest_10_minutes),
            (PERIOD_LATEST_HOUR, latest_hour),
        ):
            for observation in self._extract_observations(payload, period):
                parameter_id = observation.parameter_id

                current = parameters.get(parameter_id)
                if current is None or self._is_newer(observation, current):
                    parameters[parameter_id] = observation

        history = {
            "precip_past1min": self._normalize_history(
                self._extract_observations(
                    precip_1_minute_history,
                    "history_precip_past1min",
                )
            ),
            "precip_past10min": self._normalize_history(
                self._extract_observations(
                    precip_10_minutes_history,
                    "history_precip_past10min",
                )
            ),
            "precip_past1h": self._normalize_history(
                self._extract_observations(
                    precip_1_hour_history,
                    "history_precip_past1h",
                )
            ),
            "precip_past24h": self._normalize_history(
                self._extract_observations(
                    precip_24_hours_history,
                    "history_precip_past24h",
                )
            ),
        }

        api_timestamps = [
            timestamp
            for timestamp in (
                _parse_datetime(latest_10_minutes.get("timeStamp")),
                _parse_datetime(latest_hour.get("timeStamp")),
            )
            if timestamp is not None
        ]
        latest_observed = max(
            (observation.observed for observation in parameters.values()),
            default=None,
        )

        return ObservationSnapshot(
            station=station,
            parameters=parameters,
            history=history,
            fetched_at=datetime.now(tz=UTC),
            api_timestamp=max(api_timestamps, default=None),
            latest_observed=latest_observed,
            source_counts={
                PERIOD_LATEST_10_MINUTES: int(latest_10_minutes.get("numberReturned", 0)),
                PERIOD_LATEST_HOUR: int(latest_hour.get("numberReturned", 0)),
            },
        )

    async def _async_get_json(self, endpoint: str, params: dict[str, str]) -> dict[str, Any]:
        """Fetch JSON from DMI."""
        url = f"{BASE_URL}/{endpoint}"

        try:
            async with self._session.get(
                url,
                params=params,
                timeout=REQUEST_TIMEOUT,
            ) as response:
                if response.status == 404:
                    raise DMIStationNotFoundError("DMI endpoint returned 404")

                response.raise_for_status()
                return await response.json()
        except asyncio.TimeoutError as error:
            raise DMIConnectionError("Timed out while talking to DMI") from error
        except ClientResponseError as error:
            raise DMIConnectionError(
                f"Unexpected response from DMI: HTTP {error.status}"
            ) from error
        except ClientError as error:
            raise DMIConnectionError("Failed to reach DMI") from error

    @staticmethod
    def _is_newer(candidate: ObservationValue, current: ObservationValue) -> bool:
        """Return True if the candidate observation is newer than the current one."""
        candidate_created = candidate.created or candidate.observed
        current_created = current.created or current.observed
        return (candidate.observed, candidate_created) > (
            current.observed,
            current_created,
        )

    @staticmethod
    def _select_station_feature(features: list[dict[str, Any]]) -> dict[str, Any]:
        """Pick the current station definition from the returned history."""

        def sort_key(feature: dict[str, Any]) -> tuple[bool, bool, datetime]:
            properties = feature.get("properties", {})
            valid_from = _parse_datetime(properties.get("validFrom")) or datetime.min.replace(
                tzinfo=UTC
            )
            return (
                properties.get("status") == "Active",
                properties.get("validTo") is None,
                valid_from,
            )

        return max(features, key=sort_key)

    @staticmethod
    def _extract_observations(
        payload: dict[str, Any],
        period: str,
    ) -> list[ObservationValue]:
        """Parse observations from a payload."""
        observations: list[ObservationValue] = []

        for feature in payload.get("features", []):
            properties = feature.get("properties", {})
            parameter_id = properties.get("parameterId")
            observed = _parse_datetime(properties.get("observed"))

            if not parameter_id or observed is None:
                continue

            observations.append(
                ObservationValue(
                    parameter_id=parameter_id,
                    value=properties.get("value"),
                    observed=observed,
                    created=_parse_datetime(properties.get("created")),
                    period=period,
                )
            )

        return observations

    @staticmethod
    def _normalize_history(
        observations: list[ObservationValue],
    ) -> tuple[ObservationValue, ...]:
        """Deduplicate a history series by observation timestamp."""
        by_observed: dict[datetime, ObservationValue] = {}

        for observation in observations:
            current = by_observed.get(observation.observed)
            if current is None or DMIObservationClient._is_newer(observation, current):
                by_observed[observation.observed] = observation

        return tuple(by_observed[timestamp] for timestamp in sorted(by_observed))


def _parse_datetime(value: str | None) -> datetime | None:
    """Parse an ISO datetime from DMI."""
    if not value:
        return None

    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _format_datetime(value: datetime) -> str:
    """Format a UTC datetime for the DMI API."""
    return value.isoformat().replace("+00:00", "Z")
