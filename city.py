import os
import time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

load_dotenv()

app = FastAPI(
    title="City Time and Weather API",
    description="Create, read, and delete cities with current time and weather.",
    version="1.0.0",
)

# Create your own keys and set them before starting Uvicorn:
# $env:RAPIDAPI_KEY="your-key"
# $env:WEATHERSTACK_API_KEY="your-key"
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "")
WEATHERSTACK_API_KEY = os.getenv("WEATHERSTACK_API_KEY", "")
WORLD_TIME_URL = "https://world-time-api3.p.rapidapi.com/timezone/{timezone}"
WEATHERSTACK_URL = "https://api.weatherstack.com/current"
REQUEST_TIMEOUT_SECONDS = 10


class CityCreate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {"name": "Chicago", "timezone": "America/Chicago"}
        }
    )
    name: str = Field(min_length=1, max_length=100)
    timezone: str = Field(min_length=1, max_length=100)

    @field_validator("name", "timezone")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(
                "Use a valid IANA timezone, such as America/Chicago"
            ) from exc
        return value


class Weather(BaseModel):
    temperature_c: float
    humidity_percent: int
    condition: str
    wind_speed_kph: float


class CityResult(CityCreate):
    id: int
    current_time: str
    weather: Weather


# Four records make GET /cities/ immediately suitable for the required screenshot.
db: list[dict[str, Any]] = [
    {"id": 1, "name": "Chicago", "timezone": "America/Chicago"},
    {"id": 2, "name": "New York", "timezone": "America/New_York"},
    {"id": 3, "name": "London", "timezone": "Europe/London"},
    {"id": 4, "name": "Tokyo", "timezone": "Asia/Tokyo"},
]


def require_api_key(key: str, variable_name: str) -> None:
    if not key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Set the {variable_name} environment variable and restart Uvicorn.",
        )


def get_current_time(timezone: str) -> str:
    require_api_key(RAPIDAPI_KEY, "RAPIDAPI_KEY")
    try:
        response = requests.get(
            WORLD_TIME_URL.format(timezone=timezone),
            headers={
                "x-rapidapi-host": "world-time-api3.p.rapidapi.com",
                "x-rapidapi-key": RAPIDAPI_KEY,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.json()["datetime"]
    except (requests.RequestException, ValueError, KeyError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"World Time API failed for {timezone} ({type(exc).__name__}).",
        ) from exc


def get_weather(city_name: str) -> Weather:
    require_api_key(WEATHERSTACK_API_KEY, "WEATHERSTACK_API_KEY")
    try:
        for attempt in range(3):
            response = requests.get(
                WEATHERSTACK_URL,
                params={
                    "access_key": WEATHERSTACK_API_KEY,
                    "query": city_name,
                    "units": "m",
                },
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            if response.status_code != 429 or attempt == 2:
                break
            time.sleep(2 ** attempt)
        response.raise_for_status()
        data = response.json()
        if "error" in data:
            raise ValueError(data["error"].get("info", "Unknown Weatherstack error"))

        current = data["current"]
        descriptions = current.get("weather_descriptions") or ["Unknown"]
        return Weather(
            temperature_c=current["temperature"],
            humidity_percent=current["humidity"],
            condition=descriptions[0],
            wind_speed_kph=current["wind_speed"],
        )
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Weatherstack API failed for {city_name} ({type(exc).__name__}).",
        ) from exc


def enrich_city(city: dict[str, Any]) -> CityResult:
    return CityResult(
        **city,
        current_time=get_current_time(city["timezone"]),
        weather=get_weather(city["name"]),
    )


def find_city(city_id: int) -> dict[str, Any]:
    city = next((item for item in db if item["id"] == city_id), None)
    if city is None:
        raise HTTPException(status_code=404, detail="City not found")
    return city


@app.get("/cities/", response_model=list[CityResult], summary="Get Cities", tags=["Cities"])
def get_cities() -> list[CityResult]:
    """Return every city with its current local time and weather."""
    return [enrich_city(city) for city in db]


@app.get("/cities/{city_id}/", response_model=CityResult, summary="Get City", tags=["Cities"])
def get_city(city_id: int = Path(ge=1, description="Numeric ID of the city")) -> CityResult:
    """Return one city with its current local time and weather."""
    return enrich_city(find_city(city_id))


@app.post(
    "/cities/",
    response_model=CityResult,
    status_code=status.HTTP_201_CREATED,
    summary="Add City",
    tags=["Cities"],
)
def add_city(city: CityCreate) -> CityResult:
    """Add a city, then return it with current time and weather."""
    if any(item["name"].casefold() == city.name.casefold() for item in db):
        raise HTTPException(status_code=409, detail="City already exists")

    city_record = {
        "id": max((item["id"] for item in db), default=0) + 1,
        **city.model_dump(),
    }
    result = enrich_city(city_record)
    db.append(city_record)
    return result


@app.delete(
    "/cities/{city_id}/",
    response_model=CityResult,
    summary="Delete City",
    tags=["Cities"],
)
def delete_city(city_id: int = Path(ge=1, description="Numeric ID of the city")) -> CityResult:
    """Delete a city and return the deleted record with time and weather."""
    city = find_city(city_id)
    result = enrich_city(city)
    db.remove(city)
    return result
