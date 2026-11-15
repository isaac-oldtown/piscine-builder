"""pytest names a test module after its path (`proctor/tests/test_x.py` ->
`proctor.tests.test_x`) and would create an empty `proctor` module for it,
hiding the real package: import the real packages first."""

import coursecrafter  # noqa: F401
import proctor  # noqa: F401
