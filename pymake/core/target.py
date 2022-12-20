from enum import Enum
from functools import cached_property
from pymake.core.pathlib import Path
import time
from typing import Union, TypeAlias
import inspect

from pymake.core import asyncio, aiofiles, utils
from pymake.core.cache import SubCache
from pymake.core.errors import InvalidConfiguration
from pymake.core.settings import InstallMode, InstallSettings
from pymake.core.version import Version
from pymake.logging import Logging


class Dependencies(set):
    def __getattr__(self, attr):
        for item in self:
            if item.name == attr:
                return item

    @property
    def up_to_date(self):
        for item in self:
            if not item.up_to_date:
                return False
        return True

    @property
    def modification_time(self):
        t = 0.0
        for item in self:
            mt = item.modification_time
            if mt and mt > t:
                t = mt
        return t


TargetDependencyLike: TypeAlias = Union[list['Target'], 'Target']


PathImpl = type(Path())


class FileDependency(PathImpl):
    def __init__(self, *args, **kwargs):
        super(PathImpl, self).__init__()
        self.up_to_date = True

    @property
    def modification_time(self):
        return self.stat().st_mtime


class Option:
    def __init__(self, parent: 'Target', name: str, default) -> None:
        self.__parent = parent
        self.__cache = parent.cache
        self.fullname = f'{parent.name}.{name}'
        self.name = name
        self.__default = default
        if name == 'console_width':
            pass
        self.__value = getattr(self.__cache, self.fullname) if hasattr(
            self.__cache, self.fullname) else default
        self.__value_type = type(default)

    def reset(self):
        self.value = self.__default

    @property
    def value(self):
        return self.__value

    @value.setter
    def value(self, value):
        if self.__value_type and not isinstance(value, self.__value_type):
            err = f'option {self.fullname} is of type {self.__value_type}'
            if type(value) == str:
                if isinstance(self.__value, Enum):
                    names = [n.lower()
                             for n in self.__value_type._member_names_]
                    value = value.lower()
                    if value in names:
                        value = self.__value_type(names.index(value))
                    else:
                        err = f'option {self.fullname} should be one of {names}'
                else:
                    import json
                    value = json.loads(value)
                if not isinstance(value, self.__value_type):
                    raise RuntimeError(err)
            else:
                raise RuntimeError(err)
        if self.__value != value:
            self.__value = value
            setattr(self.__cache, self.fullname, value)
            setattr(self.__cache,
                    f'{self.__parent.fullname}.options.timestamp', time.time())


class Options:
    def __init__(self, parent: 'Target') -> None:
        self.__parent = parent
        self.__cache = parent.cache
        self.__items: set[Option] = set()

    def add(self, name: str, default_value):
        opt = Option(self.__parent, name, default_value)
        self.__items.add(opt)
        return opt

    def get(self, name: str):
        for o in self.__items:
            if name in {o.name, o.fullname}:
                return o

    @cached_property
    def modification_date(self):
        return self.__cache.get(f'{self.__parent.fullname}.options.timestamp', 0.0)

    def __getattr__(self, name):
        opt = self.get(name)
        if opt:
            return opt.value

    def __iter__(self):
        return iter(self.__items)


class Target(Logging):
    clean_request = False

    def __init__(self,
                 name: str,
                 description: str = None,
                 version: str = None,
                 parent: 'Target' = None,
                 all=True) -> None:
        from pymake.core.include import context
        self._name = name
        self.description = description
        self.version = Version(version) if version else None        
        self.parent = parent
        self.__cache: SubCache = None
        if parent is None:
            self.makefile = context.current
            self.source_path = context.current.source_path
            self.build_path = context.current.build_path
            self.options = Options(self)
        else:
            self.source_path = parent.source_path
            self.build_path = parent.build_path
            self.makefile = parent.makefile
            self.options = parent.options

        if self.version is None and hasattr(self.makefile, 'version'):
            self.version = self.makefile.version

        if self.description is None and hasattr(self.makefile, 'description'):
            self.description = self.makefile.description

        self.other_generated_files: set[Path] = set()
        self.dependencies: Dependencies[Target] = Dependencies()
        self.preload_dependencies: Dependencies[Target] = Dependencies()
        self.output: Path = None

        if self.fullname in context.all_targets:
            raise InvalidConfiguration(
                f'target {self.fullname} already exists')
        super().__init__(self.fullname)
        self.makefile.targets.add(self)
        from pymake.core.include import context
        if all:
            context.default_targets.add(self)
        context.all_targets.add(self)

    @property
    def name(self) -> str:
        return self._name

    @cached_property
    def fullname(self) -> str:
        return f'{self.makefile.name}.{self._name}'

    @property
    def cache(self) -> SubCache:
        if not self.__cache:
            self.__cache = self.makefile.cache.subcache(self.fullname)
        return self.__cache

    @asyncio.once_method
    async def preload(self):
        self.debug('preloading...')
        await asyncio.gather(*[obj.preload() for obj in self.target_dependencies])
        await asyncio.gather(*[obj.initialize() for obj in self.preload_dependencies])
        await asyncio.gather(*[obj.build() for obj in self.preload_dependencies])

    @asyncio.once_method
    async def initialize(self):
        await self.preload()
        self.debug('initializing...')

        await asyncio.gather(*[obj.initialize() for obj in self.target_dependencies])
        if self.output and not self.output.is_absolute():
            self.output = self.build_path / self.output

    def load_dependencies(self, dependencies):
        for dependency in dependencies:
            self.load_dependency(dependency)

    def load_dependency(self, dependency):
        if isinstance(dependency, Target):
            self.dependencies.add(dependency)
        elif isinstance(dependency, FileDependency):
            self.dependencies.add(dependency)
        elif isinstance(dependency, str):
            self.load_dependency(Path(dependency))
        elif isinstance(dependency, Path):
            dependency = FileDependency(self.source_path / dependency)
            self.dependencies.add(dependency)
        else:
            raise RuntimeError(
                f'Unhandled dependency {dependency} ({type(dependency)})')

    @property
    def modification_time(self):
        return self.output.stat().st_mtime if self.output.exists() else 0.0

    @property
    def up_to_date(self):
        if self.output and not self.output.exists():
            return False
        elif not self.dependencies.up_to_date:
            return False
        elif self.modification_time and self.dependencies.modification_time > self.modification_time:
            return False
        elif self.modification_time and self.modification_time < self.options.modification_date:
            return False
        return True

    @asyncio.once_method
    async def build(self):
        await self.initialize()

        if self.up_to_date:
            self.info('up to date !')
            return

        with utils.chdir(self.build_path):
            self.info('building...')
            result = self()
            if inspect.iscoroutine(result):
                return await result
            return result

    @property
    def target_dependencies(self):
        return [t for t in self.dependencies if isinstance(t, Target)]

    @property
    def file_dependencies(self):
        return [t for t in self.dependencies if isinstance(t, FileDependency)]

    @asyncio.once_method
    async def clean(self):
        await self.initialize()

        clean_tasks = [t.clean() for t in self.target_dependencies]
        if self.output and self.output.exists():
            self.info('cleaning...')
            if self.output.is_dir():
                clean_tasks.append(aiofiles.rmtree(self.output))
            else:
                clean_tasks.append(aiofiles.os.remove(self.output))
        clean_tasks.extend([aiofiles.os.remove(f)
                           for f in self.other_generated_files if f.exists()])
        try:
            await asyncio.gather(*clean_tasks)
        except FileNotFoundError as err:
            self.warning(f'file not found: {err.filename}')

    @asyncio.once_method
    async def install(self, settings: InstallSettings, mode: InstallMode):
        return

    def __call__(self):
        ...
