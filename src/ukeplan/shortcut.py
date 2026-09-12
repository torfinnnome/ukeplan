"""Apple Shortcut generation for one-tap file upload.

Builds an unsigned ``.shortcut`` file (XML plist) containing a
``Send HTTP Request`` action that POSTs the shared file to ``/api/ingest``
as multipart form data, then signs it through the public RoutineHub
HubSign service so the result is a directly importable ``AEA1`` file.

If the signing service is unreachable, the unsigned plist is returned and
the user can sign it locally with ``shortcuts sign`` on a Mac (the setup
page documents that fallback).
"""

import json
import logging
import re
import time
import xml.sax.saxutils as xu
from urllib import request as urlrequest

logger = logging.getLogger(__name__)

HUBSIGN_URL = "https://hubsign.routinehub.services/sign"
SHORTCUT_NAME = "Del til Ukeplan"
SIGN_TIMEOUT_SECONDS = 30

# In-memory cache of signed files keyed by (url, name); TTL 1 hour.
_cache: dict[str, tuple[float, bytes]] = {}
_CACHE_TTL = 3600

_PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
\t<key>WFWorkflowActions</key>
\t<array>
\t\t<dict>
\t\t\t<key>WFWorkflowActionIdentifier</key>
\t\t\t<string>com.apple.shortcuts.SendHTTPRequest</string>
\t\t\t<key>WFWorkflowActionParameters</key>
\t\t\t<dict>
\t\t\t\t<key>url</key>
\t\t\t\t<string>{url}</string>
\t\t\t\t<key>method</key>
\t\t\t\t<string>POST</string>
\t\t\t\t<key>authType</key>
\t\t\t\t<string>None</string>
\t\t\t\t<key>bodyType</key>
\t\t\t\t<string>FormData</string>
\t\t\t\t<key>formDataFields</key>
\t\t\t\t<array>
\t\t\t\t\t<dict>
\t\t\t\t\t\t<key>key</key>
\t\t\t\t\t\t<string>file</string>
\t\t\t\t\t\t<key>value</key>
\t\t\t\t\t\t<dict>
\t\t\t\t\t\t\t<key>Type</key>
\t\t\t\t\t\t\t<string>Input</string>
\t\t\t\t\t\t\t<key>VariableName</key>
\t\t\t\t\t\t\t<string>Shortcut Input</string>
\t\t\t\t\t\t</dict>
\t\t\t\t\t</dict>
\t\t\t\t\t<dict>
\t\t\t\t\t\t<key>key</key>
\t\t\t\t\t\t<string>channel</string>
\t\t\t\t\t\t<key>value</key>
\t\t\t\t\t\t<string>shortcut</string>
\t\t\t\t\t</dict>
\t\t\t\t</array>
\t\t\t\t<key>headers</key>
\t\t\t\t<array/>
\t\t\t</dict>
\t\t</dict>
\t\t<dict>
\t\t\t<key>WFWorkflowActionIdentifier</key>
\t\t\t<string>is.workflow.actions.notification</string>
\t\t\t<key>WFWorkflowActionParameters</key>
\t\t\t<dict>
\t\t\t\t<key>WFNotificationActionBody</key>
\t\t\t\t<string>Ukeplan oppdatert!</string>
\t\t\t\t<key>WFNotificationActionTitle</key>
\t\t\t\t<string>{name}</string>
\t\t\t</dict>
\t\t</dict>
\t</array>
\t<key>WFWorkflowClientVersion</key>
\t<string>26.4</string>
\t<key>WFWorkflowHasShortcutInputVariables</key>
\t<true/>
\t<key>WFWorkflowIcon</key>
\t<dict>
\t\t<key>WFWorkflowIconGlyphNumber</key>
\t\t<integer>12</integer>
\t\t<key>WFWorkflowIconStartColor</key>
\t\t<integer>463140863</integer>
\t</dict>
\t<key>WFWorkflowImportQuestions</key>
\t<array/>
\t<key>WFWorkflowInputContentItemClasses</key>
\t<array>
\t\t<string>WFGenericFileContentItem</string>
\t\t<string>WFImageContentItem</string>
\t\t<string>WFPDFContentItem</string>
\t</array>
\t<key>WFWorkflowMinimumClientVersion</key>
\t<integer>900</integer>
\t<key>WFWorkflowMinimumClientVersionString</key>
\t<string>900</string>
\t<key>WFWorkflowName</key>
\t<string>{name}</string>
\t<key>WFWorkflowNoInputBehavior</key>
\t<dict>
\t\t<key>Name</key>
\t\t<string>WFWorkflowNoInputBehaviorAskForInput</string>
\t\t<key>Prompt</key>
\t\t<string>Velg ukeplan (PDF eller bilde)</string>
\t</dict>
\t<key>WFWorkflowTypes</key>
\t<array>
\t\t<string>ActionExtension</string>
\t</array>
</dict>
</plist>
"""


def build_shortcut_plist(url: str, name: str = SHORTCUT_NAME) -> str:
    """Build the unsigned shortcut as an XML plist.

    ``url`` is the absolute endpoint (e.g. ``http://192.168.1.50:8000/api/ingest``)
    baked into the shortcut.
    """
    u = xu.escape(url, {'"': "&quot;"})
    n = xu.escape(name, {'"': "&quot;"})
    return _PLIST_TEMPLATE.format(url=u, name=n)


def sign_shortcut(plist_text: str, name: str = SHORTCUT_NAME) -> bytes | None:
    """Sign an unsigned plist via HubSign. Returns AEA1 bytes or None."""
    payload = json.dumps({"shortcutName": name, "shortcut": plist_text}).encode()
    req = urlrequest.Request(
        HUBSIGN_URL,
        data=payload,
        # HubSign's front-end only allows the Cherri user agent through.
        headers={"Content-Type": "application/json", "User-Agent": "cherri/1.0"},
    )
    try:
        with urlrequest.urlopen(req, timeout=SIGN_TIMEOUT_SECONDS) as resp:
            body = resp.read()
    except Exception:
        logger.warning("HubSign signing failed; falling back to unsigned plist", exc_info=True)
        return None
    if not body.startswith(b"AEA1"):
        logger.warning("HubSign returned unexpected payload (no AEA1 magic)")
        return None
    return body


def generate_shortcut_file(url: str, name: str = SHORTCUT_NAME) -> tuple[bytes, bool]:
    """Return (file_bytes, is_signed) for the given ingest URL."""
    key = f"{url}|{name}"
    now = time.monotonic()
    cached = _cache.get(key)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1], True

    plist_text = build_shortcut_plist(url, name)
    signed = sign_shortcut(plist_text, name)
    if signed is not None:
        _cache[key] = (now, signed)
        return signed, True
    return plist_text.encode("utf-8"), False


def shortcut_filename(name: str, is_signed: bool) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "ukeplan"
    return f"{slug}.shortcut" if is_signed else f"{slug}.plist"
