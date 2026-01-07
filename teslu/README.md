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
Compress-Archive -Path python\* -DestinationPath layer_content.zip -Force
```

# Copyright Notice

Copyright © 2026 Ting-Chen Shang. All Rights Reserved.
