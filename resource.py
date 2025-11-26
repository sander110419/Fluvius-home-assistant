"""Dummy resource module shim for Windows test runs."""

RLIMIT_NOFILE = 0


def getrlimit(_):
    """Return a safe default soft/hard limit tuple."""

    return (1024, 1024)


def setrlimit(_, limits):
    """No-op setter for file descriptor limits."""

    return None
