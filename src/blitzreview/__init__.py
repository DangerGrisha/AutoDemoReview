"""World of Tanks Blitz replay analysis (Wargaming client, `.wotbreplay` files).

A sibling of the `demoreview` (CS2) package and deliberately independent of it:
nothing here imports demoreview, and nothing needs a third-party package. The
replay is a zip, the battle results are protobuf inside a Python-2 pickle, and
the battle itself is a BigWorld packet stream -- all read with the stdlib.

Pipeline (each step is a plain function over bytes/dicts, testable alone):

    container.open_replay   .wotbreplay -> meta.json + the raw members
    battle_results.parse    battle_results.dat -> every player's final results
    packets.read_stream     data.wotreplay -> typed packet list
    arena.parse             the roster, battle periods and kill feed
    replay.build            every vehicle's track for the 2D minimap replay
    battle.load             all of the above joined into one JSON-able dict --
                            what the web app stores and the report renders

Offline only (tools/wotb_extract_assets.py, on an installed client): dvpl
unpacks client files, dava reads map scenes for the minimap geometry.

Format notes and the confidence of every decoded field: docs/WOTB_FORMAT.md.
"""
