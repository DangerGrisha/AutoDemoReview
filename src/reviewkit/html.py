"""Putting untrusted strings on a page.

Nicknames, clan tags and file names come from the uploaded file, so whoever
made it chooses them. They reach HTML only through `esc` and inline scripts
only through `embed_json`.
"""

import html
import json


def esc(value):
    """Text for HTML content or a quoted attribute; None becomes empty."""
    return html.escape("" if value is None else str(value), quote=True)


def embed_json(obj):
    """JSON for a <script type="application/json"> block. '<', '>' and '&'
    become \\u003c-style escapes -- still valid JSON that parses back
    identically -- so a nickname like '</script>' cannot end the block early."""
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))
