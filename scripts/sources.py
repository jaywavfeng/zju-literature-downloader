# -*- coding: utf-8 -*-
"""Compatibility shim -- do not add logic here.

When this skill was generalised from ZJU literature downloading to campus-SSO
resource fetching, ``sources.py`` was renamed to ``literature_sources.py``.
Two existing usage patterns must keep working unchanged:

  * ``zju_fetch.py`` does ``import sources`` and calls ``sources.fetch_plain``.
  * the documented one-liner ``python scripts/sources.py --doi ... --out ...``.

So re-export every public name from the renamed module and forward ``__main__``
to its CLI entry point.
"""
from literature_sources import *  # noqa: F401,F403

if __name__ == '__main__':
    import sys
    sys.exit(main())  # noqa: F405  (re-exported by the star import above)
