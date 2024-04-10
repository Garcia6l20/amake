import os
import re
import typing as t

from collections.abc import Iterable
from enum import Enum
from functools import cached_property

from dan.core.pathlib import Path
from dan.core import cache
from dan.core.target import Target, Installer, InstallMode
from dan.core.utils import Environment, chunks, unique
from dan.core.runners import async_run
from dan.core import asyncio
from dan.cxx.base_toolchain import CompilationFailure, LibraryList, LinkageFailure, Toolchain, CppStd, BuildType, DefaultLibraryType
from dan.core.cache import cached_property as dan_cached

import copy

class BaseTarget(Target, internal=True):

    __toolchain = 'cxx_toolchain'
    
    @property
    def toolchain(self) -> Toolchain:
        return self.context.get(self.__toolchain)


class CXXObject(BaseTarget, internal=True):
    def __init__(self, source:Path, parent: 'CXXTarget', root: Path = None) -> None:
        if source.is_absolute():
            if root is None:
                root = parent.build_path
            try:
                name = '-'.join(source.relative_to(root).with_suffix(f'').parts)
            except ValueError:
                name = '-'.join(source.relative_to(parent.build_path).with_suffix(f'').parts)
        else:
            name = '-'.join(source.with_suffix(f'').parts)
        super().__init__(name=name, parent=parent, default=False)
        self.parent = parent
        self.source = source
        obj_fname = source.with_suffix('.obj' if self.toolchain.type == 'msvc' else '.o')
        if source.is_absolute():
            if source.parent.is_relative_to(self.parent.source_path):
                rpath = source.parent.relative_to(self.parent.source_path)
                self.output = self.build_path / rpath / obj_fname.name
            elif obj_fname.is_relative_to(self.build_path):
                self.output = obj_fname
            else:
                self.output = self.build_path / obj_fname.name
        else:
            self.output = self.build_path / obj_fname
        self.__dirty = False

    @property
    def build_type(self):
        return self.parent.build_type

    @property
    def cxx_flags(self):
        return self.parent.cxx_flags

    @property
    def private_cxx_flags(self):
        return self.parent.private_cxx_flags
    
    @property
    def includes(self):
        return self.parent.includes

    @property
    def compile_definitions(self):
        return self.parent.compile_definitions
    
    @dan_cached()
    def deps(self): ...

    @dan_cached()
    def compile_args(self): ...

    async def __initialize__(self):
        await self.parent.preload()

        deps = self.deps
        if deps is not None:
            self.dependencies.update(deps)

        self.dependencies.add(self.source)

        self.other_generated_files.update(
            self.toolchain.compile_generated_files(self.output))

        previous_args = self.compile_args
        if previous_args is not None:
            args = self.toolchain.make_compile_commands(
                self.source_path / self.source, self.output, self.private_cxx_flags, self.build_type)[0]
            args = [str(arg) for arg in args]
            if sorted(args) != sorted(previous_args):
                self.__dirty = True
        else:
            self.__dirty = True

    @cached_property
    def up_to_date(self):
        if self.__dirty:
            return False
        return super().up_to_date

    async def __build__(self):
        self.info('generating %s...', self.output.name)
        try:
            self.output.parent.mkdir(parents=True, exist_ok=True)
            commands, diags = await self.toolchain.compile(self.source_path / self.source, self.output, self.private_cxx_flags, self.build_path, self.build_type)
            self.parent.diagnostics.insert(diags, str(self.source))
        except CompilationFailure as err:
            self.parent.diagnostics.insert(err.diags, str(self.source))
            err.target = self
            raise
        self.compile_args = [str(a) for a in commands[0]]
        self.debug('scanning dependencies of %s', self.source.name)
        deps = await self.toolchain.scan_dependencies(self.source_path / self.source, self.output, self.private_cxx_flags)
        deps = [d for d in deps
                if self.makefile.root.source_path in Path(d).parents
                or self.build_path in Path(d).parents]
        self.deps = deps


class OptionSet:
    def __init__(self, parent: 'CXXTarget',
                 name: str,
                 public: list | set = set(),
                 private: list | set = set(),
                 transform_out: t.Callable[[t.Any], t.Any] = None,
                 transform_in: t.Callable[[t.Any], t.Any] = None) -> None:
        self._parent = parent
        self._name = name
        self._transform_out = transform_out or self.__nop_transform
        self._transform_in = transform_in or self.__nop_transform
        self._public = list()
        self._private = list()
        self.add(*public, public=True)
        self.add(*private, public=False)

    @staticmethod
    def __nop_transform(x):
        return x

    @property
    def private(self) -> list:
        return unique(self._transform_out([self._transform_in(p) for p in self._private]))

    @property
    def public(self) -> list:
        items: list = self._transform_out([self._transform_in(p) for p in self._public])
        for dep in self._parent._recursive_dependencies((CXXTarget)):
            opts = getattr(dep, self._name)
            items.extend(opts._transform_out([opts._transform_in(p) for p in opts._public]))
        return unique(items)

    @property
    def all(self) -> list:
        items = list()
        items.extend(self.private)
        items.extend(self.public)
        return unique(items)

    @property
    def private_raw(self) -> list:
        return [self._transform_in(p) for p in self._private]

    @property
    def public_raw(self) -> list:
        return [self._transform_in(p) for p in self._public]
    
    @property
    def all_raw(self) -> list:
        return [*self.private_raw, *self.public_raw]

    def add(self, *values, public=False):
        if public:
            for value in values:
                if not value in self._public:
                    self._public.append(value)
        else:
            for value in values:
                if not value in self._private:
                    self._private.append(value)

    def update(self, values: 'OptionSet', private=False):
        self._public = values._public
        if private:
            self._private = values._private

    def extend(self, values: t.Iterable, private=False):
        if private:
            self._private.extend(values)
        else:
            self._public.extend(values)


class CXXTarget(BaseTarget, internal=True):
    public_includes: set[str] = set()
    private_includes: set[str] = set()

    public_compile_options: set[str] = set()
    private_compile_options: set[str] = set()
    
    public_compile_definitions: set[str] = set()
    private_compile_definitions: set[str] = set()

    public_lib_paths: set[str] = set()
    private_lib_paths: set[str] = set()

    public_link_libraries: set[str] = set()
    private_link_libraries: set[str] = set()

    public_link_options: set[str] = set()
    private_link_options: set[str] = set()

    build_type: BuildType = None

    __cpp_std: int|str = None

    def __make_src_path(self, path):
        if not isinstance(path, Path):
            path = Path(path)
        return path if path.is_absolute() else self.source_path / path

    def __init__(self,
                 *args,
                 **kwargs) -> None:
        super().__init__(*args, **kwargs)

        self.includes = OptionSet(self, 'includes',
                                  self.public_includes, self.private_includes,
                                  transform_out=self.toolchain.make_include_options,
                                  transform_in=self.__make_src_path)

        self.compile_options = OptionSet(self, 'compile_options',
                                         self.public_compile_options, self.private_compile_options,
                                         transform_out=self.toolchain.make_compile_options)

        self.link_libraries = OptionSet(self, 'link_libraries',
                                        self.public_link_libraries, self.private_link_libraries,
                                        transform_out=self.toolchain.make_link_options)
        
        self.library_paths = OptionSet(self, 'library_paths',
                                        self.public_lib_paths, self.private_lib_paths,
                                        transform_out=self.toolchain.make_libpath_options)

        self.compile_definitions = OptionSet(self, 'compile_definitions',
                                             self.public_compile_definitions, self.private_compile_definitions,
                                             transform_out=self.toolchain.make_compile_definitions)

        self.link_options = OptionSet(self, 'link_options',
                                      self.public_link_options, self.private_link_options)

    @property
    def cpp_std(self):
        if self.__cpp_std is None:
            self.__cpp_std = self.makefile.get_attribute('cpp_std', recursive=True)
            if self.__cpp_std is None:
                self.__cpp_std = -1
        if self.__cpp_std == -1:
            return None
        return self.__cpp_std
    
    @cpp_std.setter
    def cpp_std(self, value):
        self.__cpp_std = value

    @property
    def cxx_dependencies(self) -> list['CXXTarget']:
        return [dep for dep in self.dependencies.all if isinstance(dep, CXXTarget)]

    @property
    def library_dependencies(self) -> list['Library']:
        return [dep for dep in self.dependencies.all if isinstance(dep, Library)]

    @cached_property
    def shared_dependencies_path(self):
        paths = []
        for lib in [d for d in self.dependencies.all if isinstance(d, Library) and d.shared]:
            paths.append(lib.build_path.as_posix())
        for target in self.cxx_dependencies:
            paths.extend(target.shared_dependencies_path)
        return paths

    @cached_property
    def lib_paths(self) -> list[str]:
        tmp = set()
        for dep in self.cxx_dependencies:
            tmp.update(dep.lib_paths)
        tmp.update(self.library_paths.public)
        # # TODO move create private_libs()
        tmp.update(self.library_paths.private)
        return list(sorted(tmp))

    @cached_property
    def libs(self) -> LibraryList:
        tmp = LibraryList()
        for dep in reversed(self.cxx_dependencies):
            tmp.extend(dep.libs)
        tmp.extend(self.link_libraries.public)
        # # TODO move create private_libs()
        tmp.extend(self.link_libraries.private)
        return tmp
    
    @property
    def build_type(self):
        return self.toolchain.build_type

    @cached_property
    def cxx_flags(self):
        flags = self.includes.public
        flags.extend(self.compile_options.public)
        flags.extend(self.compile_definitions.public)
        for dep in self.cxx_dependencies:
            flags.extend(dep.cxx_flags)
        return unique(flags)

    @cached_property
    def private_cxx_flags(self):
        flags = []
        cpp_std = self.cpp_std
        if cpp_std is not None:
            flags.extend(self.toolchain.make_compile_options([cpp_std if isinstance(cpp_std, CppStd) else CppStd(cpp_std)]))
        flags.extend(self.includes.private)
        flags.extend(self.cxx_flags)
        flags.extend(self.compile_options.private)
        flags.extend(self.compile_definitions.private)
        return unique(flags)
    
    async def __install__(self, installer: Installer):
        if installer.mode == InstallMode.portable:
            
            exclude_paths = []
            if self.toolchain.system.is_windows:
                exclude_paths.append(Path(os.getenv('SYSTEMROOT')))
            def check(p: Path):
                for e in exclude_paths:
                    if e in p.parents:
                        return False
                return True
            from dan.cxx.ldd import get_runtime_dependencies
            async with asyncio.TaskGroup(f'{self.name} runtime dependencies installation') as g:
                for dep, path in await get_runtime_dependencies(self):
                    if path is not None: # not found ?
                        path = Path(path)
                        if check(path):
                            g.create_task(installer.install_bin(path))
        await super().__install__(installer)

StrOrPath = str|Path
StrOrPathIterable = Iterable[StrOrPath]

class CXXObjectsTarget(CXXTarget, internal=True):
    sources: StrOrPathIterable|t.Callable[[], StrOrPathIterable] = set()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.objs: list[CXXObject] = list()

    @cache.once_method
    def _init_sources(self):
        if callable(self.sources):
            self.sources = list(self.sources())
        if isinstance(self.sources, (str, Path)):
            self.sources = [self.sources]
        if not isinstance(self.sources, Iterable):
            assert callable(
                self.sources), f'{self.name} sources parameter should be an iterable or a callable returning an iterable'
        sources = list()
        if self.source_path != self.makefile.source_path:
            self.sources = [self.source_path / source for source in self.sources]
        
        if self.sources:
            source_root = Path(os.path.commonpath([s for s in self.sources if isinstance(s, (str, Path))]))
            for source in self.sources:
                if isinstance(source, type):
                    self.dependencies.add(source, public=False)
                    source = self.makefile.find(source).output
                else:
                    source = Path(source)
                if source.is_absolute():
                    root = source_root
                    if root.is_file():
                        root = root.parent
                else:
                    root = self.source_path
                sources.append(source)
                self.objs.append(
                    CXXObject(Path(source), self, root=root))
            self.sources = sources
            

    @property
    def file_dependencies(self):
        return unique(super().file_dependencies, *[o.file_dependencies for o in self.objs])
    
    @cached_property
    def up_to_date(self):
        for obj in self.objs:
            if not obj.up_to_date:
                return False
        return super().up_to_date

    @property
    def headers(self):
        return [f for f in self.file_dependencies if f.suffix.startswith('.h')]

    async def __initialize__(self):
        self._init_sources()
        async with asyncio.TaskGroup(f'initializing {self.name}\'s objects') as group:
            for obj in self.objs:
                group.create_task(obj.initialize())
                # self.load_dependency(obj)

    async def __build__(self):
        # compile objects
        async with self.task_group(f'building {self.name}\'s objects') as group:
            for dep in self.objs:
                group.create_task(dep.build())

    async def __clean__(self):
        async with asyncio.TaskGroup(f'cleaning {self.name}\'s objects') as group:
            for dep in self.objs:
                group.create_task(dep.clean())
        return await super().__clean__()


class LibraryType(str, Enum):
    AUTO = 'auto'
    STATIC = 'static'
    SHARED = 'shared'
    INTERFACE = 'interface'


class Library(CXXObjectsTarget, internal=True):

    header_match = r'.+'
    library_type: LibraryType = LibraryType.AUTO

    @property
    def static(self) -> bool:
        return self.library_type == LibraryType.STATIC

    @property
    def shared(self) -> bool:
        return self.library_type == LibraryType.SHARED

    @property
    def interface(self) -> bool:
        return self.library_type == LibraryType.INTERFACE

    @cached_property
    def lib_paths(self) -> list[str]:
        tmp = super().lib_paths
        if not self.interface:
            tmp.extend(self.toolchain.make_libpath_options([self.output]))
        return list(sorted(tmp))
    
    @property
    def libs(self) -> list[str]:
        if not self.interface:
            libs = LibraryList()
            libs.extend(self.toolchain.make_link_options([self.output]))
            libs.extend(super().libs)
        else:
            libs = super().libs
        
        return libs
    
    @property
    def output(self):
        if self._output is None:
            return None
        
        output_dir = None
        
        match self.library_type:
            
            case LibraryType.SHARED:
                if self.toolchain.system.is_windows:
                    output_dir = self.toolchain.settings.executable_output_dir 
                else:
                    output_dir = self.toolchain.settings.library_output_dir
                
            case LibraryType.STATIC:
                output_dir = self.toolchain.settings.archive_output_dir

        if output_dir is not None:
            return self.makefile.root.build_path / output_dir / self._output
        
        return super().output
    
    def __make_link_options(self):
        return [*self.lib_paths, *self.libs, *self.link_options.public, *self.link_options.private]


    async def __initialize__(self):
        self._init_sources()

        if self.library_type == LibraryType.AUTO:
            if len(self.sources) == 0:
                self.library_type = LibraryType.INTERFACE
            else:
                if self.toolchain.settings.default_library_type == DefaultLibraryType.static:
                    self.library_type = LibraryType.STATIC
                else:
                    self.library_type = LibraryType.SHARED

        from .msvc_toolchain import MSVCToolchain
        if self.shared and isinstance(self.toolchain, MSVCToolchain):
            self.compile_definitions.add(f'{self.name.upper()}_EXPORT=1')

        if self.library_type != LibraryType.INTERFACE:
            self._output = self.toolchain.make_library_name(self.name, self.shared)
        else:
            self._output = f"lib{self.name}.stamp"
        await super().__initialize__()

        previous_args = self.cache.get('generate_args')
        generate = None
        match self.library_type:
            case LibraryType.STATIC:
                generate = self.toolchain.static_lib
            case LibraryType.SHARED:
                generate = self.toolchain.shared_lib
        if generate is not None:
            if previous_args and \
                    previous_args != await generate(
                        [obj.routput for obj in self.objs], self.output, self.__make_link_options(), dry_run=True):
                self.__dirty = True
            else:
                self.__dirty = False
        else:
            self.__dirty = False

    @cached_property
    def up_to_date(self):
        if self.__dirty:
            return False
        return super().up_to_date

    async def __build__(self):
        await super().__build__()

        self.info(
            'creating %s library %s...', self.library_type.name.lower(), self.output.name)

        if self.static:
            await self.toolchain.static_lib([obj.routput for obj in self.objs], self.output, self.__make_link_options(), cwd=self.build_path)
        elif self.shared:
            await self.toolchain.shared_lib([obj.routput for obj in self.objs], self.output, self.__make_link_options(), cwd=self.build_path)
            from .msvc_toolchain import MSVCToolchain
            if isinstance(self.toolchain, MSVCToolchain):
                self.compile_definitions.add(
                    f'{self.name.upper()}_IMPORT=1', public=True)
        else:
            assert self.interface
            self.output.touch()

        self.debug('done')
    
    def __install_headers__(self, installer: Installer) -> list:
        tasks = list()
        header_expr = re.compile(self.header_match)
        for public_include_dir in self.includes.public_raw:
            headers = public_include_dir.rglob('*.h*')
            for header in headers:
                if header_expr.match(header.as_posix()):
                    subdirs = header.relative_to(public_include_dir).parent
                    tasks.append(installer.install_header(header, subdirs))
        return tasks
        


    async def __install__(self, installer: Installer):

        tasks = list()

        if installer.settings.create_pkg_config:
            from dan.pkgconfig.package import create_pkg_config
            tasks.append(create_pkg_config(self, installer.settings))

        if self.shared:
            tasks.append(installer.install_shared_library(self.output))
        elif self.static:
            tasks.append(installer.install_static_library(self.output))

        if installer.dev:
            tasks.extend(self.__install_headers__(installer))

            # TODO: how to handle debug symbols ? check where debug it is usually installed and do the same
            # for obj in self.objs:
            #     for dbg_file in self.toolchain.debug_files(obj.output):
            #         tasks.append(do_install(dbg_file, settings.libraries_destination / dbg_file.name))

        tasks.insert(0, super().__install__(installer))

        for tchunk in chunks(tasks, 100):
            await asyncio.gather(*tchunk)


class Module(CXXObjectsTarget, internal=True):
    def __init__(self, name: str, sources: list[str], *args, **kwargs):
        super().__init__(name, sources, *args, **kwargs)

    @property
    def cxx_flags(self):
        return {*self.toolchain.cxxmodules_flags, *super().cxx_flags}

    async def __build__(self):
        return await super().__build__()

class Executable(CXXObjectsTarget, internal=True):

    installed = True
    subsystem: str = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if self.subsystem is None:
            self.subsystem = 'console'

        self._output = self.toolchain.make_executable_name(self.name)
        self.__dirty = False

    def _make_link_options(self):
        subsystem_opt = []
        if self.toolchain.type == 'msvc':
            subsystem_opt.append(f'/subsystem:{self.subsystem}')
        return [*subsystem_opt, *self.lib_paths, *self.libs, *self.link_options.public, *self.link_options.private]

    @property
    def output(self):
        if self._output is None:
            return None
        if self.toolchain.settings.executable_output_dir is not None:
            return self.makefile.root.build_path / self.toolchain.settings.executable_output_dir / self._output
        return super().output

    @cached_property
    def env(self):
        env = Environment()

        if 'PATH' in self.toolchain.env:
            env.path_prepend(*self.toolchain.env['PATH'].split(os.pathsep))

        from dan.pkgconfig.package import get_cached_bindirs
        env.path_prepend(*get_cached_bindirs(self.context))

        env.path_prepend(self.makefile.root.pkgs_path / 'lib', var_name='LD_LIBRARY_PATH')
        
        env.path_prepend(*self.shared_dependencies_path)

        return env

    async def __initialize__(self):
        await super().__initialize__()

        previous_args = self.cache.get('link_args')
        if previous_args:
            args = self.toolchain.make_link_commands([obj.routput for obj in self.objs], self.output,
                                                     self._make_link_options())[0]
            args = [str(a) for a in args]
            if sorted(previous_args) != sorted(args):
                self.__dirty = True

    @cached_property
    def up_to_date(self):
        if self.__dirty:
            return False
        return super().up_to_date

    async def __build__(self):
        await super().__build__()

        # link
        self.info('linking %s...', self.output.name)
        try:
            commands, diags = await self.toolchain.link([obj.routput for obj in self.objs], self.output,
                                                        self._make_link_options(), cwd=self.build_path)
            self.diagnostics.insert(diags, str(self.output))
        except LinkageFailure as err:
            self.diagnostics.insert(err.diags, str(self.output))
            err.target = self
            raise
        self.cache['link_args'] = [str(a) for a in commands[0]]
        self.debug('done')

    async def __install__(self, installer: Installer):
        await installer.install_bin(self.output)
        await super().__install__(installer)

    async def execute(self, *args, build=True, **kwargs):
        if build:
            await self.build()
        return await async_run([self.output, *args], logger=self, env=self.env, **kwargs)
