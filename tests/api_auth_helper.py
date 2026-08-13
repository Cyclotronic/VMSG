"""
Attach the VMSG API token to every urllib request made by the test suites.

The control API requires a token (see vmsg_core/apiauth.py). Rather than touch
dozens of call sites, installing a global opener adds the header to all of them
and makes it impossible to forget one in a new test.

Token resolution order:
  1. VMSG_API_TOKEN environment variable  (what CI sets)
  2. api_token in the gateway's mappings.json
  3. none - requests go out unauthenticated, which is correct when the gateway
     was started with api_auth_enabled = false
"""

import json
import os
import urllib.request

TOKEN_HEADER = "X-VMSG-Token"

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFIG = os.path.join(_REPO_ROOT, "mappings.json")


def resolve_token() -> str:
    env = (os.environ.get("VMSG_API_TOKEN") or "").strip()
    if env:
        return env
    try:
        with open(_CONFIG, encoding="utf-8") as fh:
            return (json.load(fh).get("settings", {}).get("api_token") or "").strip()
    except (OSError, ValueError):
        return ""


def install() -> str:
    """Install a global opener that sends the token. Returns the token used."""
    token = resolve_token()
    if not token:
        return ""

    class _TokenProcessor(urllib.request.BaseHandler):
        # Run late so it sees the final request object.
        handler_order = 900

        def http_request(self, req):
            if not req.has_header(TOKEN_HEADER):
                req.add_header(TOKEN_HEADER, token)
            return req

        https_request = http_request

    urllib.request.install_opener(urllib.request.build_opener(_TokenProcessor()))
    return token
