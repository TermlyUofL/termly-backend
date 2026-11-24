"""Container healthcheck: exit 0 if the API answers /api/health."""
import sys
import urllib.request

try:
    urllib.request.urlopen("http://127.0.0.1:5000/api/health", timeout=4)
except Exception as exc:  # noqa: BLE001
    print(exc, file=sys.stderr)
    sys.exit(1)
