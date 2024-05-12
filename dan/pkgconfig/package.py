from copy import deepcopy
import jinja2
from dan.core import aiofiles, asyncio
from dan.core.pathlib import Path
import re
import importlib.util

from dan.core.cache import Cache
from dan.core.find import find_file, find_files, library_paths_lookup
from dan.core.pm import re_match
from dan.core.requirements import RequiredPackage, parse_requirement
from dan.core.runners import cmdline2list
from dan.core.settings import InstallMode, InstallSettings
from dan.core.utils import unique
from dan.core.version import Version, VersionSpec
from dan.cxx.targets import CXXTarget, Library
from dan.cxx.base_toolchain import LibraryList

import typing as t
import os


class MissingPackage(RuntimeError):
    def __init__(self, name) -> None:
        super().__init__(f'package {name} not found')

_pkg_config_paths = None
def _get_pkg_config_paths():
    global _pkg_config_paths
    if _pkg_config_paths is None:
        paths = os.getenv('PKG_CONFIG_PATH', None)
        if paths is None:
            _pkg_config_paths = list()
        else:
            _pkg_config_paths = [Path(p) for p in paths.split(os.pathsep)]
    return _pkg_config_paths

def find_pkg_config(name, paths: str|Path|list[str|Path] = list()) -> Path:
    if isinstance(paths, (str, Path)):
        paths = [paths]
    return find_file(fr'(lib)?{re.escape(name)}\.pc$', [*paths, *_get_pkg_config_paths(), *library_paths_lookup], re.IGNORECASE)


def find_pkg_configs(name, paths: str|Path|list[str|Path] = list()) -> t.Generator[Path, None, None]:
    if isinstance(paths, (str, Path)):
        paths = [paths]
    yield from find_files(fr'(lib)?{re.escape(name)}\.pc$', [*paths, *_get_pkg_config_paths(), *library_paths_lookup], re.IGNORECASE)


def has_package(name,  paths=list()):
    return find_pkg_config(name,  paths) is not None

def parse_package_requires(reqs):
    result = []
    reqs = [r for r in re.split(r'[\s,]', reqs) if len(r.strip()) > 0]
    tmp = []
    it = iter(reqs)
    ii = next(it, None)
    while ii is not None:
        if any(op in ii for op in ('=', '>', '<')):
            tmp.append(ii)
            ii = next(it)
            tmp.append(ii)
            result.append(parse_requirement(' '.join(tmp)))
            tmp = []
        else:
            if len(tmp):
                result.append(parse_requirement(' '.join(tmp)))
                tmp = [ii]
            else:
                tmp.append(ii)
        ii = next(it, None)
    if len(tmp):
        result.append(parse_requirement(' '.join(tmp)))
    return result

class Data:
    def __init__(self, path) -> None:
        self._path = Path(path)
        self._items = dict()
        with open(self._path) as f:
            lines = [l for l in [l.strip().removesuffix('\n')
                                 for l in f.readlines()] if len(l)]
            for line in lines:
                m = Data.__split_expr.match(line)
                if m:
                    k = m.group(1).lower()
                    v = m.group(2)
                    self._items[k] = v
        self._requires = None
        self._version = None

    __split_expr = re.compile(r'(.+?)[:=](.+)')

    def get(self, name: str, default=None):
        if not name in self._items:
            return default
        value = self._items[name]
        if type(value) == str:
            while True:
                m = re.search(r'\${(\w+)}', value)
                if m:
                    var = m.group(1)
                    value = value.replace(
                        f'${{{var}}}', self.get(var))
                else:
                    break
        return value
    
    def __getstate__(self):
        return {
            'path': self._path,
            'items': self._items,
            'requires': self._requires,
            'version': self._version,
        }
    
    def __setstate__(self, data):
        self._path = data['path']
        self._items = data['items']
        self._requires = data['requires']
        self._version = data['version']

    @property
    def path(self) -> Path:
        return self._path
    
    @property
    def requires(self) -> list[RequiredPackage]:
        if self._requires is None:
            self._requires = list()
            reqs = self.get('requires')
            if reqs is not None:
                self._requires.extend(parse_package_requires(reqs))
            reqs = self.get('requires.private')
            if reqs is not None:
                self._requires.extend(parse_package_requires(reqs))
        return self._requires
    
    @property
    def version(self) -> Version:
        if self._version is None:
            v = self.get('version')
            if v:
                self._version = Version(v)
        return self._version


class PackageConfig(CXXTarget, internal=True):
    all: dict[str, 'PackageConfig'] = dict()

    default = False

    def __init__(self, name, search_paths: list[str] = list(), config_path: Path = None, dan_plugin=None, search_plugin=True, data: Data = None, **kwargs) -> None:
        if data is not None:
            self.config_path = data.path
            self.data = data
        else:
            self.config_path = config_path or find_pkg_config(name, search_paths)
            self.data: Data = None
        self.search_paths = search_paths
        if not self.config_path:
            raise MissingPackage(name)
        if not self.config_path.parent in self.search_paths:
            self.search_paths.insert(0, self.config_path.parent)
        self.pn = name
        self.all[name] = self

        super().__init__(f'{name}-pkgconfig', **kwargs)

        if dan_plugin:
            self.__dan_plugin = dan_plugin
        elif search_plugin:
            self.__dan_plugin = find_file(rf'{name}\.py', [self.config_path.parent.parent])
        else:
            self.__dan_plugin = None
        self.__load_plugin()
        self.__cflags = None
        self.__libs = None
        self.__lib_paths = None
        self.__bin_paths = None
        if not data:
            self.data = Data(self.config_path)


        self.version = self.data.version
    
    @property
    def is_requirement(self) -> bool:
        return True

    def __load_plugin(self):
        if self.__dan_plugin is not None:
            spec = importlib.util.spec_from_file_location(
                f'{self.name}_plugin', self.__dan_plugin)
            module = importlib.util.module_from_spec(spec)
            setattr(module, 'self', self)
            spec.loader.exec_module(module)


    def __getstate__(self):
        return {
            'pn': self.pn,
            'search_paths': self.search_paths,
            'dan_plugin': self.__dan_plugin,
            'config_path': self.config_path,
            'data': self.data,
            '__cflags': self.__cflags,
            '__libs': self.__libs,
            '__lib_paths': self.__lib_paths,
            '__bin_paths': self.__bin_paths,
            'context': self.context.name,
            'makefile': self.makefile.fullname,
        }
    
    def __setstate__(self, data: dict):
        from dan.core.include import get_makefile
        makefile = get_makefile(data['context'], data['makefile'])

        self.__init__(data['pn'], data['search_paths'], data['config_path'],
                      data=data['data'],
                      dan_plugin=data['dan_plugin'],
                      search_plugin=False,
                      makefile=makefile)
        self.__cflags = data['__cflags']
        self.__libs = data['__libs']
        self.__lib_paths = data['__lib_paths']
        self.__bin_paths = data.get('__bin_paths', None)

    @property
    def modification_time(self):
        return 0.0

    @property
    def found(self):
        return True

    async def __initialize__(self):
        deps = set()
        reqs = self.data.requires
        if reqs:
            async with asyncio.TaskGroup(f'resolving {self.name}\'s requirements') as group:
                for req in reqs:
                    if req.name in self.all and req.is_compatible(self.all[req.name]):
                        dep = self.all[req.name]
                    else:
                        dep = find_package(req.name, req.version_spec, search_paths=self.search_paths, makefile=self.makefile)
                    if dep is None:
                        raise RuntimeError(f'Unresolved requirement: {req}')
                    group.create_task(dep.initialize())
                    deps.add(dep)
        self.includes.public.append(self.data.get('includedir'))
        self.dependencies.update(deps)

    @property
    def cxx_flags(self):
        if self.__cflags is None:
            cflags = self.data.get('cflags')
            if cflags is not None:
                cflags = cmdline2list(cflags)
                cflags = self.toolchain.from_unix_flags(cflags)
            else:
                cflags = list()
            for dep in self.cxx_dependencies:
                cflags.extend(dep.cxx_flags)
            self.__cflags = unique(cflags)
        return self.__cflags

    @property
    def package_dependencies(self):
        return [pkg for pkg in self.dependencies.all if isinstance(pkg, PackageConfig)]

    def __init_libs(self):
        self.__libs = LibraryList()
        self.__lib_paths = set()
        self.__bin_paths = set()
        libs = self.data.get('libs')
        if libs is not None:
            libs = cmdline2list(libs)
            libs = self.toolchain.from_unix_flags(libs)
            for l in libs:
                match re_match(l):
                    case r'-l(.+)' as m:
                        self.__libs.add(m[0])
                    case r'-L(.+)' as m:
                        self.__lib_paths.add(m[0])
                    case r'/LIBPATH:(.+)' as m:
                        self.__lib_paths.add(m[0])
                    case r'-Wl,-rpath,(.+)' as m:
                        self.__lib_paths.add(m[0])
                    case _:
                        self.__libs.add(l)

        bindir = self.data.get('bindir')
        if bindir is not None:
            self.__bin_paths.add(Path(bindir).as_posix())
        else:
            exec_prefix = self.data.get('exec_prefix')
            if exec_prefix is not None:
                bindir = Path(exec_prefix) / 'bin'
                if bindir.exists():
                    self.__bin_paths.add(bindir.as_posix())
        if self.toolchain.system.is_windows:
            # on windows dlls needs to be in PATH
            libdir = self.data.get('libdir')
            if libdir is not None:
                libdir = Path(libdir)
                for dll in find_files(r'.+.dll', [libdir]):
                    self.__bin_paths.add(dll.parent.as_posix())

        for pkg in self.package_dependencies:
            self.__lib_paths.update(pkg.lib_paths)
            self.__libs.extend(pkg.libs)
            self.__bin_paths.update(pkg.bin_paths)
        self.__lib_paths = list(sorted(self.__lib_paths))        

    @property
    def bin_paths(self) -> list[str]:
        if self.__bin_paths is None:
            self.__init_libs()
        return self.__bin_paths

    @property
    def lib_paths(self) -> list[str]:
        if self.__lib_paths is None:
            self.__init_libs()
        return self.__lib_paths
    
    @property
    def libs(self):
        if self.__libs is None:
            self.__init_libs()
        return self.__libs

    @asyncio.cached
    async def install(self, settings: InstallSettings, mode: InstallMode) -> list[Path]:
        settings = deepcopy(settings)
        settings.create_pkg_config = False
        return await super().install(settings, mode)

    @property
    def up_to_date(self):
        return True

    @property
    def modification_time(self):
        return self.config_path.modification_time

    def __getattr__(self, name):
        value = self.data.get(name) if self.data is not None else None
        if value is None:
            raise AttributeError(name)
        return value
    
    def __repr__(self):
        return f'Package[{self.name}] at {hex(id(self))}'


def get_packages_cache(context) -> dict[str, PackageConfig]:
    pkgconfig_cache = context.get('pkgconfig_cache')
    if pkgconfig_cache is None:
        pkgconfig_cache = Cache.instance(context.root.build_path / 'pkgconfig.cache', cache_name='pkgconfig', binary=True)
        context.set('pkgconfig_cache', pkgconfig_cache)
    return pkgconfig_cache.data


def find_package(name, spec: VersionSpec = None, search_paths: list = None, makefile = None):
    
    pkg = None

    if makefile is None:
        from dan.core.include import context
        makefile = context.current

    makefile = makefile.root

    cache = get_packages_cache(makefile.context)
    if name in cache:
        cached_pkg = cache[name]
        if spec and not spec.is_compatible(cached_pkg.version):
            raise RuntimeError(f'incompatible package {name} ({cached_pkg.version} {spec})')
        return cached_pkg

    search_paths = search_paths or [makefile.pkgs_path]
    for config in find_pkg_configs(name, search_paths):
        if spec is not None:
            data = Data(config)
            if spec.is_compatible(data.version):
                pkg = PackageConfig(name, data=data, makefile=makefile)
                break

        else:
            pkg = PackageConfig(name, config_path=config, search_paths=search_paths, makefile=makefile)
            break
    
    if pkg:
        cache[name] = pkg

    return pkg

def get_cached_bindirs(context):
    bindirs = context.get('bindirs')
    if bindirs is None:
        bindirs = set()
        for pkg in get_packages_cache(context).values():
            bindirs.update(pkg.bin_paths)
        
        context.set('bindirs', bindirs)

    return bindirs

_jinja_env: jinja2.Environment = None
def _get_jinja_env():
    global _jinja_env
    if _jinja_env is None:
        _jinja_env = jinja2.Environment(
            loader=jinja2.PackageLoader('dan.pkgconfig'))
    return _jinja_env


async def create_pkg_config(lib: Library, settings: InstallSettings) -> Path:
    dest = settings.data_destination / 'pkgconfig' / f'{lib.name}.pc'
    lib.info(f'creating pkgconfig: {dest}')

    requires = list()
    for dep in lib.dependencies.public:
        match dep:
            case RequiredPackage():
                if dep.version_spec:
                    requires.append(f'{dep.name} {dep.version_spec.op} {dep.version_spec.version}')
                else:
                    requires.append(dep.name)
            case Library():
                requires.append(dep.name)


    libs = list()
    if not lib.interface:
        libs.extend(lib.toolchain.make_libpath_options(
            [Path(f'${{libdir}}/{lib.name}')]))
        libs.extend(lib.toolchain.make_link_options(
            [Path(f'${{libdir}}/{lib.name}')]))
    for p in lib.library_paths.public:
        if p not in libs:
            libs.append(p)
    libs.extend(lib.link_libraries.public)
    libs.extend(lib.link_options.public)
    libs = lib.toolchain.to_unix_flags(libs)

    cflags = lib.compile_definitions.public
    cflags.extend(lib.compile_options.public)
    cflags.extend(lib.toolchain.make_include_options(['${includedir}']))
    cflags = lib.toolchain.to_unix_flags(cflags)

    data = _get_jinja_env()\
        .get_template('pkg.pc.jinja2')\
        .render({
            'lib': lib,
            'libs': libs,
            'cflags': cflags,
            'settings': settings,
            'prefix': Path(settings.destination).absolute(),
            'requires': requires
        })
    dest.parent.mkdir(parents=True, exist_ok=True)
    async with aiofiles.open(dest, 'w') as f:
        await f.write(data)
    return dest
