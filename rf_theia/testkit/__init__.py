"""rf-theia testkit — utilities for the framework's OWN self-tests.

These are NOT part of the user-facing keyword surface. They're how
rf-theia exercises its runtime in hermetic suites: synthetic event
publishers, fake clocks, etc.

Robot scenarios that load this library are SELF-TESTS of rf-theia,
not tests of theia. Keep the separation crisp.
"""
