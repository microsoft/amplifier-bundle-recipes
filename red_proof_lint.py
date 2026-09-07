"""SCRATCH ONLY -- proves the lint job goes red on a pinned-rule violation.

`os` is imported and never used: ruff F401, inside the pinned --select
E4,E7,E9,F set. Not under any testpath, so pytest never collects it and the
lint failure cannot be confused with a test failure.
"""

import os
