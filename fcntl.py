"""Dummy fcntl shim for Windows test environment."""

LOCK_EX = 0
LOCK_NB = 0
LOCK_UN = 0


def flock(fd, flags):
    """No-op flock placeholder."""

    return None
