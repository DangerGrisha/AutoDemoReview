"""Pieces both game analyzers share: demoreview (CS2) and blitzreview (WoT Blitz).

Stdlib only and no game logic -- just what each report page needs the same way:

    html       esc, embed_json: every string from an uploaded file goes onto the
               page through these
    markdown   markdown_to_html: the small, escaping markdown of coach reports
    ranking    rank_against: where a player stands in their own lobby
    theme      the palette and type both report pages are drawn in

The old names (demoreview.render.esc, blitzreview.analysis.rank_against, ...)
stay importable; they come from here now.
"""
