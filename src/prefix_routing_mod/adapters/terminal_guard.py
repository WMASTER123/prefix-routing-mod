"""Inert source and callable guards for the optional cedb477 experiment."""

import hashlib
import inspect
import json
from functools import lru_cache
from importlib.resources import files
from pathlib import Path

from .profiles import inspect_backend


@lru_cache(maxsize=1)
def terminal_profile():
    profile = json.loads(
        files(__package__).joinpath("terminal_interfaces.json").read_text("utf-8")
    )
    if (
        profile["schema_version"] != 1
        or profile["profile_id"] != "hust-cedb477-native-v1"
    ):
        raise ValueError("Unqualified terminal profile")
    return profile


def verify_installation():
    profile = terminal_profile()
    return inspect_backend(
        profile["profile_id"], additional_sources=tuple(profile["sources"].items())
    )


def verify_callable(callback, relative, qualname):
    function = getattr(callback, "__func__", callback)
    path = inspect.getsourcefile(function)
    if (
        path is None
        or hasattr(function, "__wrapped__")
        or function.__qualname__ != qualname
        or not Path(path).as_posix().endswith("/" + relative)
        or hashlib.sha256(Path(path).read_bytes()).hexdigest()
        != terminal_profile()["sources"][relative]
    ):
        raise RuntimeError("Terminal callable differs from the qualified source")


class PatchSet:
    """Restore only owned attributes; never overwrite a later foreign patch."""

    def __init__(self):
        self.entries = []

    def set(self, target, name, replacement):
        if any(obj is target and field == name for obj, field, *_ in self.entries):
            raise RuntimeError("Attribute already owned")
        present = name in vars(target)
        previous = vars(target).get(name)
        setattr(target, name, replacement)
        self.entries.append((target, name, replacement, present, previous))

    def check_current(self):
        if any(
            vars(obj).get(name) is not replacement
            for obj, name, replacement, *_ in self.entries
        ):
            raise RuntimeError("Foreign patch prevents safe restoration")

    def restore(self):
        self.check_current()
        for obj, name, _, present, previous in reversed(self.entries):
            if present:
                setattr(obj, name, previous)
            else:
                delattr(obj, name)
        self.entries.clear()
