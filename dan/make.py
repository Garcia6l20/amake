
from dataclasses import dataclass, field
from enum import Enum
import functools
import logging
import os
import fnmatch
import re

from dataclasses_json import dataclass_json
import sys
import tqdm
import typing as t

from dan.core.cache import Cache
from dan.core.makefile import MakeFile
from dan.core.pathlib import Path
from dan.core.include import include_makefile, scoped_context, Context
from dan.core import aiofiles, asyncio
from dan.core.settings import InstallMode, Settings
from dan.core.test import Test
from dan.core.utils import unique
from dan.cxx import init_toolchains
from dan.logging import Logging
from dan.core.target import Option, Target
from dan.cxx.targets import Executable
from dan.core.runners import max_jobs
from collections.abc import Iterable


def make_target_name(name: str):
    return name.replace('_', '-')


def flatten(list_of_lists):
    if len(list_of_lists) == 0:
        return list_of_lists
    if isinstance(list_of_lists[0], Iterable):
        return flatten(list_of_lists[0]) + flatten(list_of_lists[1:])
    return list_of_lists[:1] + flatten(list_of_lists[1:])

@dataclass_json
@dataclass
class Config:
    source_path: Path = None
    build_path: Path = None
    toolchain: str = None
    settings: Settings = field(default_factory=lambda: Settings())

class ConfigCache(Cache[Config]):
    indent = 4


class Make(Logging):
    _config_name = 'dan.config.json'
    _cache_name = 'dan.cache.json'

    def __init__(self, build_path: str, targets: list[str] = None, verbose: bool = False, quiet: bool = False, for_install: bool = False, jobs: int = None, no_progress=False):

        jobs = jobs or os.cpu_count()
        max_jobs(jobs)

        if quiet:
            assert not verbose, "'quiet' cannot be combined with 'verbose'"
            log_level = logging.ERROR
        elif verbose:
            log_level = logging.DEBUG
        else:
            log_level = logging.INFO
        logging.getLogger().setLevel(log_level)

        super().__init__('make')

        self.no_progress = no_progress
        self.for_install = for_install
        
        self.build_path = Path(build_path)
        self.config_path = build_path / self._config_name
        self.cache_path = build_path / self._cache_name

        self.required_targets = targets
        sys.pycache_prefix = str(build_path / '__pycache__')
        self._config = ConfigCache.instance(self.config_path)
        self.cache = Cache.instance(self.cache_path)
        
        self.debug(f'jobs: {jobs}')
        
        self.context = Context()
        
    @property
    def config(self) -> Config:
        return self._config.data
    
    @property
    def settings(self) -> Settings:
        return self._config.data.settings
    
    @property
    def source_path(self):
        return Path(self.config.source_path)
        
    @property
    def toolchain(self):
        return self.config.toolchain
    
    @property
    def root(self) -> MakeFile:
        return self.context.root

    def configure(self, source_path: str, toolchain: str = None):
        self.config.source_path = str(source_path)
        self.config.build_path = str(self.build_path)
        self.info(f'source path: {self.config.source_path}')
        self.info(f'build path: {self.config.build_path}')
        if toolchain:
            self.config.toolchain = toolchain
        if not self.config.toolchain:
            self.warning('no toolchain configured')

    @asyncio.cached
    async def initialize(self):
        assert self.config_path.exists(), 'configure first'

        self.debug(f'source path: {self.source_path}')
        self.debug(f'build path: {self.build_path}')

        toolchain = self.config.toolchain
        build_type = self.settings.build_type

        self.info(
            f'using \'{toolchain}\' toolchain in \'{build_type.name}\' mode')

        with scoped_context(self.context):
            init_toolchains(toolchain, self.settings)
            include_makefile(self.source_path, self.build_path)

        target_toolchain = self.context.get('cxx_target_toolchain')
        target_toolchain.build_type = build_type
        if self.for_install:
            library_dest = Path(self.settings.install.destination) / \
                self.settings.install.libraries_prefix
            target_toolchain.rpath = str(library_dest.absolute())

        self.debug(f'targets: {[t.name for t in self.targets]}')

    def __matches(self, target: Target | Test):
        for required in self.required_targets:
            if fnmatch.fnmatch(target.fullname, f'*{required}*'):
                return True
        return False

    @functools.cached_property
    def targets(self) -> list[Target]:
        items = list()
        if self.required_targets and len(self.required_targets) > 0:
            for target in self.root.all_targets:
                if self.__matches(target):
                    items.append(target)
        else:
            items = self.root.all_default
        return items

    @functools.cached_property
    def tests(self) -> list[Test]:
        items = list()
        if self.required_targets and len(self.required_targets) > 0:
            for required in self.required_targets:
                test_name, *test_case = required.split(':')
                test_case = test_case[0] if len(test_case) else None

                for test in self.root.all_tests:
                    
                    if fnmatch.fnmatch(test.fullname, f'*{test_name}*'):
                        if len(test) > 1 and test_case is not None:
                            cases = list()
                            for case in test.cases:
                                if fnmatch.fnmatch(case.name, test_case):
                                    cases.append(case)
                            test.cases = cases
                        
                        items.append(test)
        else:
            for test in self.root.all_tests:
                items.append(test)
        return items

    @property
    def all_options(self) -> list[Option]:
        opts = []
        for target in self.targets:
            for o in target.options:
                opts.append(o)
        for makefile in self.context.all_makefiles:
            for o in makefile.options:
                opts.append(o)
        return opts

    async def target_of(self, source):
        from dan.cxx.targets import CXXObjectsTarget

        source = Path(source)
        for target in [target for target in self.root.all_targets if isinstance(target, CXXObjectsTarget)]:            
            if target.source_path not in source.parents:
                continue
            target._init_sources()
            if source.name in target.sources:
                return target


    @classmethod
    def _parse_str_value(cls, name, value: str, orig: type, tp: type = None):
        if issubclass(orig, Enum):
            names = [n.lower()
                        for n in orig._member_names_]
            value = value.lower()
            if value in names:
                return orig(names.index(value))
            else:
                raise RuntimeError(f'{name} should be one of {names}')
        elif issubclass(orig, (set, list)):
            assert tp is not None
            result = list()
            for sub in value.split(';'):
                result.append(cls._parse_str_value(name, sub, tp))
            return orig(result)
        else:
            if tp is not None:
                raise TypeError(f'unhandled type {orig}[{tp}]')
            return orig(value)

    
    @classmethod
    def _apply_inputs(self, inputs: list[str], get_item: t.Callable[[str], tuple[t.Any, t.Any, t.Any]], info: t.Callable[[t.Any], t.Any]):
        for input in inputs:
            m = re.match(r'(.+?)([+-])?="?(.+)"?', input)
            if m:
                name = m[1]
                op = m[2]
                value = m[3]
                input, out_value, orig = get_item(name)
                sname = name.split('.')[-1]
                if orig is None:
                    orig = type(input)
                if hasattr(orig, '__annotations__') and sname in orig.__annotations__:
                    tp = orig.__annotations__[sname]
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
                in_value = self._parse_str_value(name, value, orig, tp)
                match (out_value, op, in_value):
                    case (list()|set(), '-', list()|set()) if len(in_value) == 1 and list(in_value)[0] == '*':
                        out_value.clear()
                    case (set(), '+', set()):
                        out_value.update(in_value)
                    case (set(), '-', set()):
                        out_value = out_value - in_value
                    case (list(), '+', set()):
                        out_value.insert(in_value)
                    case (list(), '-', set()):
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
                info(name, out_value)
            else:
                raise RuntimeError(f'cannot process given input: {input}')

    async def apply_options(self, *options):
        await self.initialize()
        all_opts = self.all_options
        def get_option(name):
            for opt in all_opts:
                if opt.fullname == name:
                    return opt.cache, opt.value, opt.type
        self._apply_inputs(options, get_option, lambda k, v: self.info(f'option: {k} = {v}'))

    async def apply_settings(self, *settings):
        await self.initialize()
        def get_setting(name):
            parts = name.split('.')
            setting = self.settings
            for part in parts[:-1]:
                if not hasattr(setting, part):
                    raise RuntimeError(f'no such setting: {name}')
                setting = getattr(setting, part)
            if not hasattr(setting, parts[-1]):
                raise RuntimeError(f'no such setting: {name}')
            value = getattr(setting, parts[-1])
            return setting, value, type(setting)
        self._apply_inputs(settings, get_setting, lambda k, v: self.info(f'setting: {k} = {v}'))


    @staticmethod
    def toolchains():
        from dan.cxx.detect import get_toolchains
        return get_toolchains()

    class progress:

        def __init__(self, desc, targets, task_builder, disable) -> None:
            self.desc = desc
            self.targets = targets
            self.builder = task_builder
            import shutil
            term_cols = shutil.get_terminal_size().columns
            self.max_desc_width = int(term_cols * 0.25)
            self.pbar = tqdm.tqdm(total=len(targets),
                                  desc='building', disable=disable)
            self.pbar.unit = ' targets'

        def __enter__(self):
            def update(n=1):
                desc = self.desc + ' ' + \
                    ', '.join([t.name for t in self.targets])
                if len(desc) > self.max_desc_width:
                    desc = desc[:self.max_desc_width] + ' ...'
                self.pbar.set_description_str(desc)
                self.pbar.update(n)
            update(0)

            def on_done(t: Target, *args, **kwargs):
                self.targets.remove(t)
                update()

            tasks = list()

            for t in self.targets:
                tsk = asyncio.create_task(self.builder(t))
                tsk.add_done_callback(functools.partial(on_done, t))
                tasks.append(tsk)

            return tasks

        def __exit__(self, *args):
            self.pbar.set_description_str(self.desc + ' done')
            self.pbar.refresh()
            return

    async def build(self):
        await self.initialize()

        with scoped_context(self.context), \
             self.progress('building', self.targets, lambda t: t.build(), self.no_progress) as tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            errors = list()
            for result in results:
                if isinstance(result, Exception):
                    self._logger.exception(result)
                    errors.append(result)
            err_count = len(errors)
            if err_count == 1:
                raise errors[0]
            elif err_count > 1:
                raise RuntimeError('One or more targets failed, check log...')

    async def install(self, mode: InstallMode = InstallMode.user):
        await self.initialize()

        self.for_install = True
        await self.initialize()
        targets = self.root.all_installed

        await self.build()

        with scoped_context(self.context), \
             self.progress('installing', targets, lambda t: t.install(self.settings.install, mode), self.no_progress) as tasks:
            installed_files = await asyncio.gather(*tasks)
            installed_files = unique(flatten(installed_files))
            manifest_path = self.settings.install.data_destination / \
                'dan' / f'{self.root.name}-manifest.txt'
            manifest_path.parent.mkdir(parents=True, exist_ok=True)

            async with aiofiles.open(manifest_path, 'w') as f:
                await f.writelines([os.path.relpath(p, manifest_path.parent) + '\n' for p in installed_files])

    @property
    def executable_targets(self) -> list[Executable]:
        return [exe for exe in self.targets if isinstance(exe, Executable)]

    async def scan_toolchains(self, script: Path = None):
        from dan.cxx.detect import create_toolchains, load_env_toolchain
        if script:
            load_env_toolchain(script)
        else:
            create_toolchains()

    async def run(self):
        await self.initialize()
        with scoped_context(self.context):
            results = await asyncio.gather(*[t.execute(log=True) for t in self.executable_targets])
            for result in results:
                if result[2] != 0:
                    return result[2]
        return 0

    async def test(self):
        await self.initialize()
        with scoped_context(self.context):
            with self.progress('testing', self.tests, lambda t: t.run_test(), self.no_progress) as tasks:
                results = await asyncio.gather(*tasks)
                if all(results):
                    self.info('Success !')
                    return 0
                else:
                    self.error('Failed !')
                    return 255

    async def clean(self):
        await self.initialize()
        with scoped_context(self.context):
            await asyncio.gather(*[t.clean() for t in self.targets])
            # from dan.cxx import target_toolchain
            # target_toolchain.compile_commands.clear()
