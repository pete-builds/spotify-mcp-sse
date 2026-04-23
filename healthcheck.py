"""Health check script for Docker HEALTHCHECK."""

import os
import sys
import urllib.request


def check():
    port = os.getenv("MCP_PORT", "3703")
    try:
        resp = urllib.request.urlopen(f"http://localhost:{port}/sse", timeout=5)
        if resp.status == 200:
            sys.exit(0)
    except Exception:
        pass
    sys.exit(1)


if __name__ == "__main__":
    check()
