import asyncio
import os
import pprint
import aiohttp
import boto3
from loguru import logger
import requests
import math

from tesla_fleet_api import TeslaFleetApi

SSM_PREFIX = "/teslu"

# The specific AU Fleet API endpoint you requested
TESLA_REGION = "na"
TESLA_REGION_BASE_URL = "https://fleet-api.prd.na.vn.cloud.tesla.com"
TESLA_AUTH_URL = "https://auth.tesla.com/oauth2/v3/token"


def get_secrets():
    ssm = boto3.client("ssm")

    # Fetch all secrets in one call (limit is 10 per call, we are requesting 6)
    ssm_parameter_names = {
        "client_id",
        "client_secret",
        "refresh_token",
        "private_key",
        "vin",
        "home/effective_radius",
        "home/latitude",
        "home/longitude",
    }
    response = ssm.get_parameters(
        Names=[f"{SSM_PREFIX}/{name}" for name in ssm_parameter_names],
        WithDecryption=True,
    )

    secrets = {}
    for p in response["Parameters"]:
        key = p["Name"].removeprefix(f"{SSM_PREFIX}/")
        secrets[key] = p["Value"]

    # Validate critical keys exist
    missing_keys = {k for k in secrets.keys() if k not in ssm_parameter_names}
    if missing_keys:
        raise Exception(f"Missing required SSM parameters: {missing_keys}")

    return secrets


def get_access_token(secrets):
    payload = {
        "grant_type": "refresh_token",
        "client_id": secrets["client_id"],
        "refresh_token": secrets["refresh_token"],
    }

    response = requests.post(TESLA_AUTH_URL, data=payload)

    if response.status_code != 200:
        logger.error(f"Token Refresh Failed: {response.text}")
        raise Exception("Could not refresh token")

    return response.json()["access_token"]


def is_at_home(current_lat, current_lon, home_lat, home_lon, home_radius_meters=10.0):
    # Haversine formula
    R = 6371230  # Earth radius in meters
    phi1 = math.radians(home_lat)
    phi2 = math.radians(current_lat)
    d_phi = math.radians(current_lat - home_lat)
    d_lambda = math.radians(current_lon - home_lon)

    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    distance = R * c

    logger.debug(f"Distance from home: {int(distance)} meters.")
    return distance < home_radius_meters


async def async_main(event: dict[str, str]):
    logger.info("0. Determine target state...")
    target_state = event.get("sentry", "on").lower()
    logger.debug(f"Target Sentry State: {target_state}")

    logger.info("1. Fetching secrets...")
    secrets = get_secrets()

    # Create temp key file for the signing library
    tmp_directory = "./tmp"
    key_path = f"{tmp_directory}/private_key.pem"
    os.makedirs(tmp_directory, exist_ok=True)
    with open(key_path, "w") as f:
        f.write(secrets["private_key"])

    access_token = get_access_token(secrets)

    # Initialize Tesla Fleet API (Handles Token Refresh & Signing)
    try:
        async with aiohttp.ClientSession() as session:
            api = TeslaFleetApi(
                session=session,
                access_token=access_token,
                region=TESLA_REGION,  # type: ignore[arg-type] # mypy doesnt like Literal types :shrug:
            )
            await api.get_private_key(key_path)
            signed = api.vehicles.createSigned(vin=secrets["vin"])
            vehicle_data = (
                await signed.vehicle_data(
                    endpoints=["location_data", "vehicle_state", "drive_state"]
                )
            )["response"]

            is_sentry_mode_available = vehicle_data["vehicle_state"][
                "sentry_mode_available"
            ]
            is_sentry_mode_on = vehicle_data["vehicle_state"]["sentry_mode"]
            latitude = vehicle_data["drive_state"]["latitude"]
            longitude = vehicle_data["drive_state"]["longitude"]
            shift_state = vehicle_data["drive_state"].get("shift_state")

            logger.info("2. Validating vehicle state...")
            if not is_sentry_mode_available:
                logger.warning("Sentry Mode NOT available on this vehicle. Skipping.")
                return {"status": "Skipped", "reason": "Sentry Mode not available"}

            desired_sentry_status = target_state == "on"
            if is_sentry_mode_on == desired_sentry_status:
                logger.info(f"Sentry Mode already {target_state}. No action needed.")
                return {"status": "Skipped", "reason": "Already in desired state"}

            if shift_state and shift_state != "P":
                logger.warning(f"Car is not in Park ({shift_state=}). Skipping.")
                return {"status": "Skipped", "reason": "Car not in Park"}

            logger.info("3. Validating geofence...")
            home_radius = float(secrets["home/effective_radius"])
            home_lat = float(secrets["home/latitude"])
            home_lon = float(secrets["home/longitude"])
            if not is_at_home(latitude, longitude, home_lat, home_lon, home_radius):
                logger.info("Car is NOT at home. No action taken.")
                return {"status": "Skipped", "reason": "Not at home"}

            logger.info(f"4. Setting Sentry Mode to: {target_state.upper()}...")
            response = (await signed.set_sentry_mode(on=desired_sentry_status))[
                "response"
            ]
            if not response["result"]:
                logger.error(f"Failed to set Sentry Mode: {pprint.pformat(response)}")
                return {"status": "Error", "reason": "API command failed."}

            logger.info(f"Sentry Mode set successfully to {target_state.upper()}.")
            return {"status": "Success", "sentry_mode": target_state}

    finally:
        if os.path.exists(key_path):
            os.remove(key_path)


def lambda_handler(event, context):
    return asyncio.run(async_main(event))
