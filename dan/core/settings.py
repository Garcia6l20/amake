from dataclasses import dataclass, field
from enum import Enum
import re

from dan.core.pathlib import Path
import dan.core.typing as t


class InstallMode(str, Enum):
    user = 'user'
    dev = 'dev'
    portable = 'portable'


class BuildType(str, Enum):
    debug = 'debug'
    release = 'release'
    release_min_size = 'release_min_size'
    release_debug_infos = 'release_debug_infos'

    @property
    def is_debug_mode(self):
        """Return true if the build type should produce debug symbols (ie.: debug and release_debug_infos)"""
        return self in (BuildType.debug, BuildType.release_debug_infos)

@dataclass(eq=True, unsafe_hash=True)
class InstallSettings:
    destination: str = '/usr/local'
    runtime_prefix: str = 'bin'
    libraries_prefix: str = 'lib'
    includes_prefix: str = 'include'
    data_prefix:str = 'share'
    create_pkg_config: bool = True

    @property
    def runtime_destination(self):
        return Path(self.destination).absolute() / self.runtime_prefix

    @property
    def libraries_destination(self):
        return Path(self.destination).absolute() / self.libraries_prefix

    @property
    def data_destination(self):
        return Path(self.destination).absolute() / self.data_prefix

    @property
    def includes_destination(self):
        return Path(self.destination).absolute() / self.includes_prefix

@dataclass(eq=True, unsafe_hash=True)
class BuildSettings:
    toolchain: str = None
    install: InstallSettings = field(default_factory=lambda: InstallSettings())
    config: t.Any = None


def safe_load(name: str, value,  t: type):
    if t is not None and not isinstance(value, t):
        err = f'value {name} should be of type {t}'
        if type(value) == str:
            if issubclass(t, Enum):
                names = [n.lower()
                         for n in t._member_names_]
                value = value.lower()
                if value in names:
                    value = t(names.index(value))
                else:
                    err = f'option {name} should be one of {names}'
            else:
                import json
                value = json.loads(value)
            if not isinstance(value, t):
                raise RuntimeError(err)
        else:
            raise RuntimeError(err)
    return value

def _parse_str_value(name, value: str, orig: type, tp: type = None):
    if issubclass(orig, Enum):
        names = [n.lower()
                    for n in orig._member_names_]
        value = value.lower()
        if value in names:
            return orig._value2member_map_[value]
        else:
            raise RuntimeError(f'{name} should be one of {names}')
    elif issubclass(orig, (set, list)):
        if tp is None:
            tp = str # assume str
        result = list()
        for sub in value.split(';'):
            result.append(_parse_str_value(name, sub, tp))
        return orig(result)
    elif orig == bool:
        return value.lower() in ('true', 'yes', 'on', '1')
    elif value.lower() in ('none', 'null', 'undefined'):
        return None
    else:
        if tp is not None:
            raise TypeError(f'unhandled type {orig}[{tp}]')
        return orig(value)

def _apply_inputs(inputs: list[str], get_item: t.Callable[[str], tuple[t.Any, t.Any, t.Any]], logger = None, input_type_name='setting'):
    for input in inputs:
        m = re.match(r'(.+?)([+-])?="?(.+)"?', input)
        if m:
            name = m[1]
            op = m[2]
            value = m[3]
            _item = get_item(name)
            if _item is None and logger is not None:
                logger.warning('unknown %s: %s (skipped)', input_type_name, name)
                continue
            input, out_value, orig = _item
            sname = name.split('.')[-1]
            if orig is None:
                orig = type(input)
            if hasattr(orig, '__annotations__') and sname in orig.__annotations__:
                tp = orig.__annotations__[sname]
                if t.is_optional(tp):
                    orig = t.get_args(tp)[0]
                    tp = None
                else:
                    orig = t.get_origin(tp)
                    if orig is None:
                        orig = tp
                        tp = None
                    else:
                        args = t.get_args(tp)
                        if args:
                            tp = args[0]
            else:
                tp = None
            in_value = _parse_str_value(name, value, orig, tp)
            match (out_value, op, in_value):
                case (list()|set(), '-', list()|set()) if len(in_value) == 1 and list(in_value)[0] == '*':
                    out_value.clear()
                case (set(), '+', set()):
                    out_value.update(in_value)
                case (set(), '-', set()):
                    out_value = out_value - in_value
                case (list(), '+', set()|list()):
                    out_value.extend(in_value)
                case (list(), '-', set()|list()):
                    for v in in_value:
                        out_value.remove(v)
                case (_, '+' | '-', _):
                    raise TypeError(f'unhandled "{op}=" operator on type {type(out_value)} ({name})')
                case _:
                    out_value = in_value
            if isinstance(input, dict):
                input[sname] = out_value
            else:
                setattr(input, sname, out_value)
            if logger is not None:
                logger.info('%s: %s = %s', input_type_name, name, out_value)
        else:
            logger.warning('cannot process given input: %s', input)


def apply_settings(base, *settings, logger=None):
    def get_setting(name):
        parts = name.split('.')
        setting = base
        for part in parts[:-1]:
            if not hasattr(setting, part):
                raise RuntimeError(f'no such setting: {name}')
            setting = getattr(setting, part)
        part = parts[-1]
        if not hasattr(setting, part):
            if isinstance(setting, dict) and part in setting:
                value = setting[part]
                return setting, value, type(value)
            else:
                raise RuntimeError(f'no such setting: {name}')
        else:
            value = getattr(setting, part)
        return setting, value, type(setting)
    _apply_inputs(settings, get_setting, logger=logger)
