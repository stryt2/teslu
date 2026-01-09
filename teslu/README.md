# Teslu Application

This is a private Tesla python application for personal use.

Tesla official app does not support sentry mode scheduling based on geofence, and sentry mode
drains battery pretty quickly if left on all the time.

This lambda function allows me to turn on and off sentry mode based on other things like
EventBridge.

# Create Lambda Layer using UV

For reference: https://docs.astral.sh/uv/guides/integration/aws-lambda/#using-a-lambda-layer

But with some tweaks because it seems like the layer structure created as above does not work.

1. Install dependencies directly into `python` folder (as a flat structure) instead of
 `python/lib/site-packages`:

```powershell
uv export --frozen --no-dev --no-editable -o requirements.txt
uv pip install `
  --no-installer-metadata `
  --no-compile-bytecode `
  --python-platform x86_64-manylinux2014 `
  --python 3.14 `
  --target python `
  -r requirements.txt
```

2. Zip the `python` folder contents into `layer_content.zip`:

```powershell
Compress-Archive -Path python -DestinationPath layer_content.zip -Force
```

# Manually Fetching Tesla Refresh Token

Go to the link below in your browser:

https://auth.tesla.com/oauth2/v3/authorize?client_id=[CLIENT_ID]&locale=en-US&prompt=login&redirect_uri=https%3A%2F%2Fapp.teslu.store%2Fcallback&response_type=code&scope=openid%20offline_access%20user_data%20vehicle_device_data%20vehicle_cmds%20vehicle_charging_cmds%20vehicle_location&state=[RANDOM_STATE_STRING]

```powershell
$TokenBody = @{
  grant_type    = "authorization_code"
  client_id     = "<CLIENT_ID>"
  client_secret = "<CLIENT_SECRET>"
  code          = "<AUTHORIZATION_CODE>"  # The code you get from the URL after log in when visiting the above link
  audience      = "https://fleet-api.prd.na.vn.cloud.tesla.com"
  redirect_uri  = "https://app.teslu.store/callback"
}

$Response = Invoke-RestMethod -Uri "https://auth.tesla.com/oauth2/v3/token" -Method Post -Body $TokenBody

$Response
```

# Copyright Notice

Copyright © 2026 Ting-Chen Shang. All Rights Reserved.
