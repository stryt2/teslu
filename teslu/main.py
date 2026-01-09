import asyncio
import math
import os
import pprint
import time
from typing import Any

import aiohttp
import boto3
from dotenv import load_dotenv
import requests
from loguru import logger
from retry import retry
from tesla_fleet_api import TeslaFleetApi
from tesla_fleet_api.exceptions import TeslaFleetError


SSM_PREFIX = "/teslu"

# The specific AU Fleet API endpoint you requested
TESLA_REGION = "na"
TESLA_REGION_BASE_URL = "https://fleet-api.prd.na.vn.cloud.tesla.com"
TESLA_AUTH_URL = "https://auth.tesla.com/oauth2/v3/token"

# AWS Parameter Store
IS_AWS_LAMBDA = "LAMBDA_TASK_ROOT" in os.environ

load_dotenv()
ssm = (
    boto3.client("ssm")
    if IS_AWS_LAMBDA
    else boto3.Session(
        profile_name=os.getenv("AWS_PROFILE"),
        region_name=os.getenv("AWS_REGION"),
    ).client("ssm")
)


# A custom exception to signal retrying later so that the retry decorator can catch it and retry.
class RetryLater(Exception):
    pass


def get_secrets():
    # Fetch all secrets in one call (limit is 10 per call, we are requesting 8)
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
        raise Exception(f"Token Refresh Failed: {response.text}")

    data = response.json()
    access_token = data["access_token"]

    new_refresh_token = data.get("refresh_token")

    if new_refresh_token and new_refresh_token != secrets["refresh_token"]:
        logger.debug("Refresh token has been rotated. Updating SSM Parameter...")
        try:
            ssm.put_parameter(
                Name=f"{SSM_PREFIX}/refresh_token",
                Value=new_refresh_token,
                Type="SecureString",
                Overwrite=True,
            )
        except Exception as e:
            logger.warning(f"Failed to update refresh token in SSM: {e}")

    return access_token


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


def get_temp_directory() -> str:
    return (
        "/tmp"  # this is the only writable directory in AWS Lambda
        if IS_AWS_LAMBDA
        else "./tmp"
    )


def is_vehicle_in_service(vehicle: dict) -> bool:
    return vehicle["in_service"]


def is_sentry_mode_available(vehicle_data: dict) -> bool:
    return vehicle_data["vehicle_state"]["sentry_mode_available"]


def is_sentry_mode_on(vehicle_data: dict) -> bool:
    return vehicle_data["vehicle_state"]["sentry_mode"]


def is_vehicle_in_park(vehicle_data: dict) -> bool:
    return vehicle_data["drive_state"]["shift_state"] == "P"


async def wake_up_vehicle(signed, prev_vehicle) -> dict[str, Any]:
    round, max_rounds, vehicle = 0, 5, prev_vehicle
    while vehicle["state"] != "online" and round < max_rounds:
        round += 1
        round_tag = f"{round}/{max_rounds}"
        logger.debug(f"Vehicle is not online. Sending wake_up command... ({round_tag})")
        await signed.wake_up()

        # According to the doc, it may take 10-60s for the vehicle to connect.
        t_0 = time.time()
        while time.time() - t_0 <= 65.0:  # 60 seconds (plus a buffer) max wait
            await asyncio.sleep(5)
            vehicle = (await signed.vehicle())["response"]
            if vehicle["state"] == "online":
                logger.debug(
                    f"Vehicle is now online after {int(time.time() - t_0)}s. ({round_tag})"
                )
                break
            else:
                logger.debug(
                    f"Vehicle ({vehicle['state']=}) is still not online after "
                    f"{int(time.time() - t_0)}s. Waiting for another 5s... ({round_tag})"
                )

    if vehicle["state"] != "online":
        logger.error(
            f"Vehicle failed to come online after {round} wake_up tries. Aborting."
        )
        raise RetryLater(
            f"Vehicle failed to come online after {round} wake_up tries. Aborting."
        )

    return vehicle


async def async_main(event: dict[str, str]):
    logger.info("Determine target state...")
    target_state = event.get("sentry", "on").lower()
    if target_state not in {"on", "off"}:
        logger.error(f"Invalid target Sentry Mode state: {target_state}")
        return {"status": "Error", "reason": "Invalid target state"}
    logger.debug(f"Target Sentry State: {target_state}")

    logger.info("Fetching secrets...")
    secrets = get_secrets()

    # Create temp key file for the signing library
    tmp_directory = get_temp_directory()
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

            vehicle = (await signed.vehicle())["response"]

            logger.info("Ensuring vehicle is not in service mode...")
            if is_vehicle_in_service(vehicle):
                logger.warning("Vehicle is in service mode. Skipping.")
                return {"status": "Skipped", "reason": "Vehicle in service mode"}

            logger.info("Ensuring vehicle is online...")
            vehicle = await wake_up_vehicle(signed, vehicle)

            # Now that the vehicle is online, fetch the vehicle data.
            vehicle_data = (
                await signed.vehicle_data(
                    endpoints=[
                        "charge_state",
                        "location_data",
                        "vehicle_state",
                        "drive_state",
                    ]
                )
            )["response"]

            logger.info("Ensuring Sentry Mode is available...")
            if not is_sentry_mode_available(vehicle_data):
                logger.warning("Sentry Mode NOT available on this vehicle. Skipping.")
                return {"status": "Skipped", "reason": "Sentry Mode not available"}

            logger.info("Checking current Sentry Mode status vs. target state...")
            if is_sentry_mode_on(vehicle_data) == (target_state == "on"):
                logger.info(f"Sentry Mode already {target_state}. No action needed.")
                return {"status": "Skipped", "reason": "Already in desired state"}

            # Do different validation based on target state. Reason being that if we are turning on
            # Sentry Mode, we don't need to check geofence and shift state because we are increasing
            # security.
            if target_state == "on":
                # TODO: Do battery level check
                logger.info(
                    "Skipping geofence and shift state check for turning ON Sentry Mode..."
                )
            else:
                logger.info("Ensuring vehicle is in Park...")
                if not is_vehicle_in_park(vehicle_data):
                    shift_state = vehicle_data["drive_state"]["shift_state"]
                    logger.warning(f"Car is not in Park ({shift_state=}). Skipping.")
                    return {"status": "Skipped", "reason": "Car not in Park"}

                logger.info("Ensuring vehicle is at home...")
                if not is_at_home(
                    current_lat=vehicle_data["drive_state"]["latitude"],
                    current_lon=vehicle_data["drive_state"]["longitude"],
                    home_lat=float(secrets["home/latitude"]),
                    home_lon=float(secrets["home/longitude"]),
                    home_radius_meters=float(secrets["home/effective_radius"]),
                ):
                    logger.info("The car is NOT at home. No action taken.")
                    return {"status": "Skipped", "reason": "Not at home"}

            logger.info(
                f"All validations passed. Setting Sentry Mode to: {target_state.upper()}..."
            )
            on = target_state == "on"
            response = (await signed.set_sentry_mode(on=on))["response"]
            if not response["result"]:
                logger.error(f"Failed to set Sentry Mode: {pprint.pformat(response)}")
                raise RetryLater(
                    f"Failed to set Sentry Mode: {pprint.pformat(response)}"
                )

            logger.info(f"Sentry Mode set successfully to {target_state.upper()}.")
            return {"status": "Success", "sentry_mode": target_state}

    finally:
        if os.path.exists(key_path):
            os.remove(key_path)


# Retry up to 5 times on TeslaFleetError and RetryLater, sleep 1, 2, 4, 8, 16 seconds between
# attempts.
@retry((TeslaFleetError, RetryLater), tries=5, delay=1, backoff=2)
def lambda_handler(event, context):
    return asyncio.run(async_main(event))


# For local testing.
if __name__ == "__main__":
    result = lambda_handler(dict(sentry="on"), None)
    pprint.pprint(result)
