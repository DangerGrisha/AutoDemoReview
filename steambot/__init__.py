"""steambot -- proof-of-concept: automatic CS2 match detection + demo download.

Standalone, deliberately NOT wired into the parsing pipeline / web app / DB.
Goal of this pass: prove end-to-end for a single account that we can
  1. detect a new match (sharecode chain via the Steam Web API), and
  2. resolve + download the raw .dem file (via the CS2 Game Coordinator).

See README.md in this package for setup and the honest protocol status write-up.
"""

__all__ = ["__version__"]
__version__ = "0.1.0-poc"
