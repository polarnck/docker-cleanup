#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""docker_cleanup.py - audit Docker disk usage and remove only what was approved.

The implementation lives in the ``docker_cleanup_lib`` package next to this
file.  This entry point keeps ``python3 scripts/docker_cleanup.py`` working.

Exit codes: 0 OK, 2 usage/environment, 3 safety refusal, 4 partial failure,
5 nothing to do.
"""

import sys

from docker_cleanup_lib.cli import main


if __name__ == "__main__":
    sys.exit(main())
