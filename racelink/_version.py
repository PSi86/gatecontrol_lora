"""Canonical RaceLink Host version helpers."""

VERSION = "0.1.0"
__version__ = VERSION


def get_version() -> str:
    return VERSION


def print_version() -> None:
    print(VERSION)
