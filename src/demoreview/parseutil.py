"""Small shared helpers for reading demoparser2 output safely.

Lives in its own module so both `parsing` and `grenades` can use it without an
import cycle (parsing already imports grenades).
"""

try:
    import pandas as pd
except ImportError:                      # parsing.py reports this properly
    pd = None


def event_df(parser, name, **kwargs):
    """`parser.parse_event(...)` normalised to a DataFrame, or None.

    demoparser2 returns a bare **list** (usually `[]`), not an empty DataFrame,
    when a demo holds no events of that type at all -- a match where nobody
    planted, nobody flashed, or buy time never ended. Callers then hit
    `.empty` / `.sort_values` on a list and the whole parse dies with
    `'list' object has no attribute 'empty'`, throwing away a good match because
    one event kind was missing. Seen in the wild on a real matchmaking demo.
    """
    df = parser.parse_event(name, **kwargs)
    if isinstance(df, list):
        return pd.DataFrame(df) if (df and pd is not None) else None
    return df
