import importlib.util
from contextlib import contextmanager
import os
import sys

from dan.core.asyncio import sync_wait
from dan.core.makefile import MakeFile
from dan.core.pathlib import Path
from dan.core.requirements import load_requirements
from dan.core.target import Target
from dan.logging import Logging
from dan.pkgconfig.package import parse_requirement
from dan.core.settings import BuildSettings


class TargetNotFound(RuntimeError):
    def __init__(self, name) -> None:
        super().__init__(f'package {name} not found')


def requires(*requirements) -> list[Target]:
    ''' Requirement lookup

    1. Searches for a target exported by a previously included makefile
    2. Searches for pkg-config library

    :param names: One (or more) requirement(s).
    :return: The list of found targets.
    '''
    # return [parse_requirement(req) for req in requirements]
    global context
    requirements = [parse_requirement(req) for req in requirements]
    sync_wait(load_requirements(requirements, makefile=context.current, install=False))
    return requirements


class Context(Logging):

    _all: list['Context'] = []
    
    def __init__(self, name = None, settings: BuildSettings = None):
        self.name = name
        self.__root: MakeFile = None
        self.imported_makefiles: dict[Path, MakeFile] = dict()
        self.__ctx_stack: list[Context] = []
        self.__makefile_stack: list[MakeFile] = []
        self.__attributes = dict()
        self.settings = settings

        Context._all.append(self)

    @staticmethod
    def find_global(context_name: str, makefile_name: str):
        for ctx in Context._all:
            if ctx.name == context_name:
                for makefile in ctx.all_makefiles:
                    if makefile.fullname == makefile_name:
                        return makefile
    
    @property
    def root(self):
        return self.__root

    @property
    def current(self):
        return self.__makefile_stack[-1] if self.__makefile_stack else None

    @property
    def all_makefiles(self) -> set[MakeFile]:
        return self.imported_makefiles.values()


    def get(self, name, default=None):
        if name in self.__attributes:
            return self.__attributes[name]
        if default is not None:
            self.__attributes[name] = default
            return default

    def set(self, name, value):
        self.__attributes[name] = value

    def __enter__(self):
        global context
        self.__ctx_stack.append(context)
        context = self
        return self

    def __exit__(self, *exc):
        global context
        context = self.__ctx_stack.pop()
        assert context is not None

    @contextmanager
    def _init_makefile(self, module, name: str = 'root', build_path: Path = None, requirements: MakeFile = None, parent: MakeFile = None, is_requirement=False):
        source_path = Path(module.__file__).parent

        if self.__root is None:
            self.__root = module

        if parent is None and self.current is not None:
            parent = self.current

        if build_path is None:
            assert parent is not None
            build_path = build_path or parent.build_path / name

        module.__class__ = MakeFile
        self.__makefile_stack.append(module)
        module._setup(
            name,
            source_path,
            build_path,
            requirements,
            parent,
            is_requirement,
            self)
        yield module
        self.__makefile_stack.pop()


# TODO: remove me !!!
context: Context = Context()

class MakeFileError(RuntimeError):
    def __init__(self, path) -> None:
        self.path = Path(path)
        super().__init__(f'failed to load {self.path}')


def load_makefile(module_path: Path,
                  name: str = None,
                  module_name: str = None,
                  build_path: Path = None,
                  requirements: MakeFile = None,
                  parent: MakeFile = None,
                  is_requirement=False) -> MakeFile:
    name = name or module_path.stem
    module_name = module_name or name

    if module_path in context.imported_makefiles:
        return context.imported_makefiles[module_path]
    spec = importlib.util.spec_from_file_location(
        module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    context.imported_makefiles[module_path] = module
    with context._init_makefile(module, name, build_path, requirements, parent, is_requirement):
        try:
            spec.loader.exec_module(module)
        except Exception as err:
            context.error('makefile error while loading \'%s\': %s', module_path, err)
            raise MakeFileError(module_path) from err
    return module


def include_makefile(name: str | Path, build_path: Path = None) -> set[Target]:
    ''' Include a sub-directory (or a sub-makefile).
    :returns: The set of exported targets.
    '''
    global context
    if not context.root:
        assert type(name) == type(Path())
        module_path: Path = name / 'dan-build.py'
        name = 'root'
        spec = importlib.util.spec_from_file_location(
            f'{context.name}.{name}', module_path)
    else:
        lookups = [
            os.path.join(name, 'dan-build.py'),
            f'{name}.py',
        ]
        for lookup in lookups:
            module_path = context.current.source_path / lookup
            if module_path.exists():
                spec = importlib.util.spec_from_file_location(
                    f'{context.name}.{context.current.name}.{name}', module_path)
                break
        else:
            raise RuntimeError(
                f'Cannot find anything to include for "{name}" (looked for: {", ".join(lookups)})')
        
    if module_path in context.imported_makefiles:
        return context.imported_makefiles[module_path]
    
    module_python_path = str(module_path.parent)
    if module_python_path not in sys.path:
        sys.path.append(module_python_path)

    module = importlib.util.module_from_spec(spec)
    context.imported_makefiles[module_path] = module

    with context._init_makefile(module, name, build_path):
        requirements_file = module_path.with_stem('dan-requires')
        if module_path.stem == 'dan-build' and requirements_file.exists():
            context.current.requirements = load_makefile(
                requirements_file, name='dan-requires', module_name=f'{context.name}/{name}.requirements', is_requirement=True)

        try:
            spec.loader.exec_module(module)
        except TargetNotFound as err:
            if len(context.missing) == 0:
                raise err
        except Exception as err:
            context.error('makefile error while including %s: %s', module_path, err)
            raise MakeFileError(module_path) from err


def include(*names: str | Path) -> list[Target]:
    """Include one (or more) subdirectory (or named makefile).

    :param names: One (or more) subdirectory or makefile to include.
    :return: The list of targets exported by the included targets.
    """
    for name in names:
        include_makefile(name)

def get_makefile(context_name: str, makefile_name: str):
    return Context.find_global(context_name, makefile_name)
