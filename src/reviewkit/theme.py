"""The palette and type both report pages share.

A tactical near-black UI: the same dark panels, lines and text on the CS2
match report, the Blitz battle report and the site (web/static/app.css keeps
the same values by hand). Each game adds its own accents on top -- CT blue and
T orange for CS2, ally green, enemy red and "you" gold for Blitz.
"""

BASE = {
    "bg": "#0B0E11",
    "panel": "#12161B",
    "panel2": "#171C22",
    "line": "#232A31",
    "text": "#E4E7EA",
    "muted": "#8A929B",
}

FONTS = {
    "display": '"Rajdhani","Oswald","Segoe UI",system-ui,sans-serif',
    "body": '"Inter",-apple-system,"Segoe UI",Roboto,Arial,sans-serif',
    "mono": '"JetBrains Mono",ui-monospace,Menlo,monospace',
}

GOOGLE_FONTS = ("https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700"
                "&family=JetBrains+Mono:wght@400;500;600;700&family=Rajdhani:wght@500;600;700"
                "&display=swap")


def css_vars(names=None, fonts=True, sep=";"):
    """The shared tokens as CSS custom properties, `--bg:#0B0E11;...`.
    `names` renames a token for a page that calls it differently (the CS2
    report says --card for --panel)."""
    names = names or {}
    tokens = dict(BASE, **(FONTS if fonts else {}))
    return sep.join(f"--{names.get(key, key)}:{value}" for key, value in tokens.items())
