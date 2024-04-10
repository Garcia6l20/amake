import collections
import contextlib
from functools import cached_property
import functools
from dan.core.register import MakefileRegister
from dan.core.pathlib import Path
from typing import Any, Callable, Iterable, Union, TypeAlias
import inspect

from dan.core import asyncio, aiofiles, utils, diagnostics as diags
from dan.core.requirements import load_requirements
from dan.core.settings import InstallMode, InstallSettings, safe_load
from dan.core.version import Version
from dan.logging import Logging
from dan.core.terminal import TermStream


class Dependencies:

    def __init__(self, parent: 'Target', public: Iterable = None, private: Iterable = None):
        super().__init__()
        self.parent = parent
        self._public = list()
        self._private = list()
        if public is not None:
            self.update(public, public=True)
        if private is not None:
            self.update(private, public=False)

    @property
    def makefile(self):
        return self.parent.makefile

    def add(self, dependency, public=True):
        content = self._public if public else self._private
        if dependency in content:
            return
        from dan.pkgconfig.package import RequiredPackage
        match dependency:
            case Target() | FileDependency():
                content.append(dependency)
            case type():
                assert issubclass(dependency, Target)
                dep = self.makefile.find(dependency)
                if dep is None:
                    raise RuntimeError(f'cannot find dependency class: {dependency.__name__}')
                content.append(dep)
            case str():
                from dan.pkgconfig.package import PackageConfig
                for pkg in PackageConfig.all.values():
                    if pkg.name == dependency:
                        content.append(pkg)
                        break
                else:
                    if isinstance(self.parent.source_path, Path) and Path(self.parent.source_path / dependency).exists():
                        content.append(FileDependency(
                            self.parent.source_path / dependency))
                    else:
                        from dan.pkgconfig.package import parse_requirement
                        content.append(parse_requirement(dependency))
            case Path():
                dependency = FileDependency(
                    self.parent.source_path / dependency)
                content.append(dependency)
            case RequiredPackage():
                content.append(dependency)
            case _:
                raise RuntimeError(
                    f'Unhandled dependency {dependency} ({type(dependency)})')

    def update(self, dependencies, public=True):
        match dependencies:
            case Dependencies():
                for dep in dependencies.public:
                    self.add(dep, public=True)
                for dep in dependencies.private:
                    self.add(dep, public=False)
            case collections.abc.Iterable():
                for dep in dependencies:
                    self.add(dep, public=public)
            case _:
                raise RuntimeError('unhandled')

    def __getattr__(self, attr):
        for item in self._public:
            if item.name == attr:
                return item
        for item in self._private:
            if item.name == attr:
                return item
    
    @property
    def public(self):
        return self._public.__iter__()

    @property
    def private(self):
        return self._private.__iter__()

    @property
    def all(self):
        yield from self.private
        yield from self.public

    @property
    def up_to_date(self):
        for item in self.all:
            if not item.up_to_date:
                return False
        return True

    @property
    def modification_time(self):
        t = 0.0
        for item in self.all:
            mt = item.modification_time
            if mt and mt > t:
                t = mt
        return t


TargetDependencyLike: TypeAlias = Union[list['Target'], 'Target']


PathImpl = type(Path())


class FileDependency(PathImpl):
    
    def __init__(self, *args, **kwargs):
        super(PathImpl, self).__init__()

    @property
    def up_to_date(self):
        return self.exists()

    @property
    def modification_time(self):
        return self.stat().st_mtime


class Option:
    def __init__(self, parent: 'Options', fullname: str, default, help: str = None) -> None:
        self.fullname = fullname
        self.name = fullname.split('.')[-1]
        self.__parent = parent
        self.__cache = parent._cache
        self.__default = default
        self.__value = self.__cache.get(self.name, default)
        self.__value_type = type(default)
        self.__help = help if help is not None else 'No description.'

    def reset(self):
        self.value = self.__default

    @property
    def parent(self):
        return self.__parent

    @property
    def cache(self):
        return self.__cache

    @property
    def type(self):
        return self.__value_type

    @property
    def default(self):
        return self.__default

    @property
    def help(self):
        return self.__help

    @property
    def value(self):
        return self.__value

    @value.setter
    def value(self, value):
        value = safe_load(self.fullname, value, self.__value_type)
        if self.__value != value:
            self.__value = value
            self.__cache[self.name] = value


class Options:
    def __init__(self, parent: 'Target', default: dict[str, Any] = dict()) -> None:
        self.__parent = parent
        cache = parent.cache
        if isinstance(parent.cache, dict):
            if not parent.name in cache:
                cache[parent.name] = dict()
            cache = cache[parent.name]
        else:
            cache = parent.cache.data
        if not 'options' in cache:
            cache['options'] = dict()
        self._cache = cache['options']
        self.__items: list[Option] = list()
        self.update(default)

    def add(self, name: str, default_value, help=None):
        if self.get(name, False) is not None:
            raise RuntimeError(f'duplicate options detected ({name})')
        opt = Option(self, f'{self.__parent.fullname}.{name}',
                     default_value, help=help)
        self.__items.append(opt)
        return opt

    def get(self, name: str, parent_lookup = True):
        for o in self.__items:
            if name in {o.name, o.fullname}:
                return o
        if parent_lookup and self.__parent.parent is not None:
            return self.__parent.parent.options.get(name)

    def update(self, options: dict):
        for k, v in options.items():
            help = None
            match v:
                case dict():
                    help = v['help']
                    v = v['default']
                case tuple() | list() | set():
                    help = v[1]
                    v = v[0]
                case _:
                    pass
            if self[k]:
                self[k] = v
            else:
                self.add(k, v, help)
    
    @property
    def sha1(self):
        import hashlib
        sha1 = hashlib.sha1()
        for o in self.__items:
            sha1.update(o.fullname.encode() + str(o.value).encode())
        return sha1.hexdigest()

    def items(self):
        for o in self.__items:
            yield o.name, o.value

    def __getattr__(self, name):
        opt = self.get(name)
        if opt:
            return opt.value

    def __getitem__(self, name):
        opt = self.get(name)
        if opt:
            return opt.value

    def __iter__(self):
        return iter(self.__items)

class Installer:
    def __init__(self, settings: InstallSettings, mode: InstallMode, logger: Logging) -> None:
        self.settings = settings
        self.mode = mode
        self.installed_files = list()
        self._logger = logger
    
    async def _install(self, src: Path|str, dest: Path, subdir: Path = None):
        if subdir is not None:
            dest /= subdir
        if isinstance(src, Path):
            dest /= src.name
            if dest.exists() and dest.younger_than(src):
                self._logger.info('%s is up-to-date', dest)
                self.installed_files.append(dest)
                return
            self._logger.debug('installing: %s', dest)
            await aiofiles.copy(src, dest)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            self._logger.debug('installing: %s', dest)
            async with aiofiles.open(dest, 'w') as f:
                await f.write(src)
        self.installed_files.append(dest)

    @property
    def dev(self):
        return self.mode == InstallMode.dev
    
    async def install_bin(self, src, subdir = None):
        await self._install(src, self.settings.runtime_destination, subdir)

    async def install_shared_library(self, src, subdir = None):
        await self._install(src, self.settings.libraries_destination, subdir)

    async def install_static_library(self, src, subdir = None):
        if not self.dev:
            return
        await self._install(src, self.settings.libraries_destination, subdir)

    async def install_header(self, src, subdir = None):
        if not self.dev:
            return
        await self._install(src, self.settings.includes_destination, subdir)

    async def install_data(self, src, subdir = None, dev=False):
        if dev and not self.dev:
            return
        await self._install(src, self.settings.data_destination, subdir)


class Target(Logging, MakefileRegister, internal=True):
    name: str = None
    fullname: str = None
    description: str = None
    default: bool = True
    installed: bool = False
    output: Path = None
    options: dict[str, Any]|Options = dict()
    provides: Iterable[str] = None

    dependencies: Dependencies = set()
    public_dependencies: set[TargetDependencyLike] = set()
    private_dependencies: set[TargetDependencyLike] = set()

    preload_dependencies: Dependencies = set()

    inherits_version = True
    subdirectory: str = None

    __cache_nop_codec = lambda x: x

    @staticmethod
    def root_cached(name, encode=None, decode=None, get_fn=None):
        """Create new property for cached variable (root scope)"""
        encode = encode or Target.__cache_nop_codec
        decode = decode or Target.__cache_nop_codec
        def get(obj):
            result = obj.makefile.root.cache.data.get(name)
            if result is not None:
                return decode(result)
            elif get_fn is not None:
                result = get_fn(obj)
                if result is not None:
                    obj.makefile.root.cache.data[name] = encode(result)
                return result

        def set(obj, value):
            obj.makefile.root.cache.data[name] = encode(value)
        return property(get, set)

    @staticmethod
    def root_cached_property(name, encode=None, decode=None):
        """Create new property for cached variable (root scope)"""
        def wrapper(get_fn):
            return Target.root_cached(name, encode, decode, get_fn)
        return wrapper
    
    @staticmethod
    def makefile_cached(name, encode=None, decode=None, get_fn=None):
        """Create new property for cached variable (makefile scope)"""
        encode = encode or Target.__cache_nop_codec
        decode = decode or Target.__cache_nop_codec
        def get(obj):
            result = obj.makefile.cache.data.get(name)
            if result is not None:
                return decode(result)
            elif get_fn is not None:
                result = get_fn(obj)
                if result is not None:
                    obj.makefile.cache.data[name] = encode(result)
                return result
        def set(obj, value):
            obj.makefile.cache.data[name] = encode(value)
        return property(get, set)

    @staticmethod
    def makefile_cached_property(name, encode=None, decode=None):
        """Create new property for cached variable (makefile scope)"""
        def wrapper(get_fn):
            return Target.makefile_cached(name, encode, decode, get_fn)
        return wrapper
    
    @staticmethod
    def target_cached(name, encode=None, decode=None, get_fn=None):
        """Create new property for cached variable (target scope)"""
        encode = encode or Target.__cache_nop_codec
        decode = decode or Target.__cache_nop_codec
        def get(obj):
            result = obj.cache.get(name)
            if result is not None:
                return decode(result)
            elif get_fn is not None:
                result = get_fn(obj)
                if result is not None:
                    obj.cache.data[name] = encode(result)
                return result
        def set(obj, value):
            obj.cache[name] = encode(value)
        return property(get, set)
        
    @staticmethod
    def target_cached_property(name, encode=None, decode=None):
        """Create new property for cached variable (target scope)"""
        def wrapper(get_fn):
            return Target.target_cached(name, encode, decode, get_fn)
        return wrapper

    def __init__(self,
                 name: str = None,
                 parent: 'Target' = None,
                 version: str = None,
                 default: bool = None,
                 makefile=None) -> None:
        
        self.parent = parent
        self.__cache: dict = None
        self.__source_path : Path = None

        if name is not None:
            self.name = name

        if self.name is None:
            self.name = self.__class__.__name__
        
        if self.provides is None:
            self.provides = {self.name}
        else:
            self.provides = set(self.provides)

        if default is not None:
            self.default = default

        if makefile:
            self.makefile = makefile
        elif parent is not None:
            self.makefile = parent.makefile
        elif self.makefile is None:
            from dan.core.include import context
            self.makefile = context.current
            if self.makefile is None:
                raise RuntimeError('Makefile not resolved')

        if parent is not None:
            self.fullname = f'{parent.fullname}.{self.name}'
            self._stream = parent._stream.sub(self.display_name)
        else:
            self._stream = TermStream(self.display_name)



        if self.fullname is None:
            self.fullname = f'{self.context.name}.{self.makefile.fullname}.{self.name}'

        self.options = Options(self, self.options)

        if version is not None:
            self._version = version

        if not hasattr(self, '_version'):
            if self.inherits_version:
                self._version = self.makefile.version
            else:
                self._version = None

        if self.description is None:
            self.description = self.makefile.description

        self.other_generated_files: set[Path] = set()

        deps = self.dependencies
        self.dependencies = Dependencies(self, self.public_dependencies, self.private_dependencies)
        self.dependencies.update(deps)
        self.preload_dependencies = Dependencies(
            self, None, self.preload_dependencies)

        self._output: Path = None
        self._build_path = None
        
        if inspect.isclass(self.source_path) and issubclass(self.source_path, Target):
            # class-defined source -> delayed resolution
            def _get_source_path(TargetClass, self):
                return self.get_dependency(TargetClass).output
            self.preload_dependencies.add(self.source_path, public=False)
            type(self).source_path = property(functools.partial(_get_source_path, self.source_path))
        elif isinstance(self.source_path, Target):
            # target-defined source -> delayed resolution
            def _get_source_path(target, self):
                return target.output
            self.preload_dependencies.add(self.source_path, public=False)
            type(self).source_path = property(functools.partial(_get_source_path, self.source_path))

        # if type(self).output != Target.output:
        #     # hack class-defined output
        #     #   transform it to classproperty for build_path resolution
        #     output = self.output
        #     type(self).output = utils.classproperty(lambda: self.build_path / output)

        self.diagnostics = diags.DiagnosticCollection()

    @property
    def output(self):
        if self._output is None:
            return None
        return self.build_path / self._output

    @property
    def routput(self):
        return self._output
    
    @property
    def version(self):
        version = self._version
        if isinstance(version, Option):
            version = version.value
        if isinstance(version, str):
            version = Version(version)
        return version

    @version.setter
    def version(self, value):
        self._version = value

    @output.setter
    def output(self, path):
        path = Path(path)
        if not path.is_absolute() and self.build_path in path.parents:
            raise RuntimeError(f'output must not be an absolute path within build directory')
        elif path.is_absolute() and self.build_path in path.parents:
            self._output = path.relative_to(self.build_path)
        else:
            self._output = path

    @property
    def is_requirement(self) -> bool:
        return self.makefile.is_requirement

    @property
    def source_path(self) -> Path:
        if self.__source_path is None:
            return self.makefile.source_path
        else:
            return self.__source_path
    
    @source_path.setter
    def source_path(self, value):
        self.__source_path = value

    @property
    def build_path(self) -> Path:
        if self._build_path is not None:
            return self._build_path
        
        build_path = self.makefile.build_path

        if self.subdirectory is not None:
            build_path /= self.subdirectory

        return build_path
        
    
    @property
    def requires(self):
        from dan.pkgconfig.package import RequiredPackage
        return [dep for dep in self.dependencies.all if isinstance(dep, RequiredPackage)]
    
    @cached_property
    def display_name(self) -> str:
        return f'{self.context.name}/{self.name}'

    @property
    def cache(self) -> dict:
        if not self.__cache:
            name = self.fullname.removeprefix(self.makefile.fullname + '.')
            if not name in self.makefile.cache.data:
                self.makefile.cache.data[name] = dict()
            self.__cache = self.makefile.cache.data[name]
        return self.__cache
    
    _install_missing_dependencies = True

    def _recursive_dependencies(self, types = None, seen = None):
        if seen is None:
            seen = set()
        for dep in self.dependencies.all:
            if dep in seen:
                continue
            seen.add(dep)
            if types is None or isinstance(dep, types):
                yield dep
                if isinstance(dep, Target):
                    yield from dep._recursive_dependencies(types, seen)

    @property
    @contextlib.contextmanager
    def skip_missing_dependencies(self):
        self._install_missing_dependencies = False
        yield
        self._install_missing_dependencies = True
    
    @property
    def status(self):
        return self._stream.status

    @property
    def task_group(self):
        return self._stream.task_group
    
    @property
    def progress(self):
        return self._stream.progress
    
    def hide_output(self):
        self._stream.hide()

    async def __load_unresolved_dependencies(self, install=None):
        if install is None:
            install = self._install_missing_dependencies
        if len(self.requires) > 0:
            self.dependencies.update(await load_requirements(self.requires, name=self.name, makefile=self.makefile, logger=self, install=install))

    @asyncio.cached
    async def preload(self):
        self.trace('preloading...')

        async with asyncio.TaskGroup(f'building {self.name}\'s preload dependencies') as group:
            group.create_task(self.__load_unresolved_dependencies())
            for dep in self.preload_dependencies.all:
                group.create_task(dep.build())

        async with asyncio.TaskGroup(f'preloading {self.name}\'s target dependencies') as group:
            for dep in self.target_dependencies:
                group.create_task(dep.preload())

        return await asyncio.may_await(self.__preload__())

    @asyncio.cached
    async def load_dependencies(self):
        async with asyncio.TaskGroup(f'loading {self.name}\'s dependencies') as group:
            group.create_task(self.__load_unresolved_dependencies(install=False))

    @asyncio.cached
    async def initialize(self):
        await self.preload()
        self.trace('initializing...')

        if isinstance(self.version, Option):
            self.version = self.version.value

        async with asyncio.TaskGroup(f'initializing {self.name}\'s target dependencies') as group:
            for dep in self.target_dependencies:
                group.create_task(dep.initialize())

        return await asyncio.may_await(self.__initialize__())

    @property
    def modification_time(self):
        output = self.build_path / f'{self.name}.stamp' if self.output is None else self.output  
        return output.stat().st_mtime if output.exists() else 0.0

    @cached_property
    def up_to_date(self):
        output = self.build_path / f'{self.name}.stamp' if self.output is None else self.output
        if output and not output.exists():
            return False
        elif not self.dependencies.up_to_date:
            return False
        elif self.dependencies.modification_time > self.modification_time:
            return False
        elif 'options_sha1' in self.cache and self.cache['options_sha1'] != self.options.sha1:
            return False
        return True

    async def _build_dependencies(self):
        if not self.target_dependencies:
            return
        async with self.task_group('building dependencies...') as group:
            for dep in self.target_dependencies:
                group.create_task(dep.build())

    @asyncio.cached
    async def build(self):
        await self.initialize()

        await self._build_dependencies()

        result = await asyncio.may_await(self.__prebuild__())

        if self.up_to_date:
            self.status('up to date !', icon='✔', timeout=1)
            self.trace('up to date !')
            if self.is_requirement:
                self.hide_output()
            return
        elif self.output is not None and self.output.exists():
            self.debug('outdated !')

        with utils.chdir(self.build_path):
            self.status('building...')
            self.debug('building...')
            if diags.enabled:
                self.diagnostics.clear()
            try:
                if self.output is not None:
                    self.output.parent.mkdir(exist_ok=True, parents=True)
                result = await asyncio.may_await(self.__build__())
                if self.output is None:
                    (self.build_path / f'{self.name}.stamp').touch()
                self.cache['options_sha1'] = self.options.sha1
                self.trace('built')
                self.status('built', icon='✔')
                self._stream.hide_children()
                if self.is_requirement:
                    self.hide_output()
                return result
            except Exception as err:
                msg = f'failed: {err}'
                self.status(msg, icon='✘')
                self.error(msg)
                raise err


    @property
    def target_dependencies(self):
        return [t for t in {*self.dependencies.all, *self.preload_dependencies.all} if isinstance(t, Target)]

    @property
    def file_dependencies(self):
        return [t for t in self.dependencies.all if isinstance(t, FileDependency)]

    @asyncio.cached
    async def clean(self):
        await self.initialize()
        async with asyncio.TaskGroup(f'cleaning {self.name} outputs') as group:
            output = self.build_path / f'{self.name}.stamp' if self.output is None else self.output
            if output and output.exists():
                self.info('debug...')
                if output.is_dir():
                    group.create_task(aiofiles.rmtree(output, force=True))
                else:
                    group.create_task(aiofiles.os.remove(output))
            for f in self.other_generated_files:
                if f.exists():
                    group.create_task(aiofiles.os.remove(f))
            group.create_task(asyncio.may_await(self.__clean__()))

    @asyncio.cached(unique = True)
    async def install(self, settings: InstallSettings, mode: InstallMode):
        await self.build()

        self.debug('installing %s to %s', self.name, settings.destination)

        installer = Installer(settings, mode, self)
        await self.__install__(installer)
        return installer.installed_files


    def __get_dependency(self, dep: str | type, recursive=True) -> TargetDependencyLike:
        """Search for dependency"""
        if isinstance(dep, str):
            def check(d): return d.name == dep
        else:
            def check(d): return isinstance(d, dep)
        for dependency in self.dependencies.all:
            if check(dependency):
                return dependency
        for dependency in self.preload_dependencies.all:
            if check(dependency):
                return dependency
        if recursive:
            # not found... look for dependencies' dependencies
            for target in self.target_dependencies:
                dependency = target.__get_dependency(dep)
                if dependency is not None:
                    return dependency

    def get_dependency(self, dep: str | type, recursive=True) -> TargetDependencyLike:
        dependency = self.__get_dependency(dep, recursive)
        from dan.core.requirements import RequiredPackage
        match dependency:
            case RequiredPackage():
                if dependency.target is not None:
                    return dependency.target
                else:
                    return dependency
            case _:
                return dependency

    async def __preload__(self):
        ...

    async def __initialize__(self):
        ...

    async def __prebuild__(self):
        ...

    async def __build__(self):
        ...

    async def __install__(self, installer: Installer):
        if installer.dev:
            if len(self.utils) > 0:
                body = str()
                for fn in self.utils:
                    tmp = inspect.getsourcelines(fn)[0]
                    tmp[0] = f'\n\n@self.utility\n'
                    body += '\n'.join(tmp)
                await installer.install_data(body, f'dan/{self.name}.py')

    async def __clean__(self):
        ...

    @utils.classproperty
    def utils(cls) -> list:
        utils_name = f'_{cls.__name__}_utils__'
        if not hasattr(cls, utils_name):
            setattr(cls, utils_name, list())
        return getattr(cls, utils_name)

    @classmethod
    def utility(cls, fn: Callable):
        cls.utils.append(fn)
        name = fn.__name__
        makefile = cls.get_static_makefile()
        if makefile is not None:
            inst = makefile.find(cls)
            fn = functools.partial(fn, inst)
        setattr(cls, name, fn)
        return fn

    async def run(self, command, cwd=None, env=None, **kwargs):
        from dan.core.runners import async_run
        kwargs['logger'] = self
        if cwd is None:
            cwd = self.build_path
        if env is None:
            env = getattr(self, 'env', None)
        return await async_run(command, cwd=cwd, env=env, **kwargs)
