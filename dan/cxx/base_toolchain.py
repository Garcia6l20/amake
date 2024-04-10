from enum import Enum
import dan.core.diagnostics as diag
from dan.core.pathlib import Path
from dan.core.settings import BuildType
from dan.core.target import FileDependency
from dan.core.runners import async_run, sync_run, CommandError
from dan.core.version import Version
from dan.logging import Logging
from dan.cxx.compile_commands import CompileCommands
from dan.core.toolchains import BaseToolchain, Lang

from dataclasses import dataclass, field

import dan.core.typing as t

import tempfile

CommandArgs = list[str|Path]
CommandArgsList = list[CommandArgs]

class RuntimeType(Enum):
    static = 0
    dynamic = 1

class DefaultLibraryType(str, Enum):
    static = 'static'
    shared = 'shared'

@dataclass(eq=True, unsafe_hash=True)
class ToolchainSettings:
    build_type: BuildType = BuildType.debug
    compile_flags: list[str] = field(default_factory=lambda: list(), compare=False)
    link_flags: list[str] = field(default_factory=lambda: list(), compare=False)
    default_library_type: DefaultLibraryType = DefaultLibraryType.static
    executable_extension: t.Optional[str] = None
    archive_extension: t.Optional[str] = None
    library_extension: t.Optional[str] = None
    position_independent_code: bool = True
    executable_output_dir: t.Optional[str] = "bin"
    archive_output_dir: t.Optional[str] = "lib"
    library_output_dir: t.Optional[str] = "lib"

class CppStd:
    def __init__(self, stdver: int|str) -> None:
        self.stdver = stdver


class LibraryList:
    def __init__(self, *items: t.Iterable):
        self._lst = list()
        self.extend(items)
    
    def add(self, item):
        if not item in self._lst:
            self._lst.append(item)

    def extend(self, items):
        for item in items:
            try:
                self._lst.remove(item)
            except ValueError:
                pass
        self._lst.extend(items)

    def __iter__(self):
        return iter(self._lst)
    
    def __reversed__(self):
        return reversed(self._lst)


class BaseFailure(RuntimeError):
    def __init__(self, msg: str, err: CommandError, options: set[str], command: str, toolchain: 'Toolchain', diags: list[diag.Diagnostic], target = None) -> None:
        super().__init__(msg)
        self.options = options
        self.command = command
        self.toolchain = toolchain
        self.stdout = err.stdout
        self.stderr = err.stderr
        self.diags = diags
        self.target = target
    

class CompilationFailure(BaseFailure):
    def __init__(self, err: CommandError, sourcefile: Path, options: set[str], command: str, toolchain: 'Toolchain', diags: list[diag.Diagnostic] = [], target = None) -> None:
        super().__init__(f'failed to compile {sourcefile}: {err.stdout}{err.stderr}', err, options, command, toolchain, diags, target)
        self.sourcefile = sourcefile


class LinkageFailure(BaseFailure):
    def __init__(self, err: CommandError, objects: set[Path], options: set[str], command: str, toolchain: 'Toolchain', diags: list[diag.Diagnostic] = [], target = None) -> None:
        super().__init__(f'failed to link {", ".join([str(o) for o in objects])}: {err.stdout}{err.stderr}', err, options, command, toolchain, diags, target)
        self.objects = objects

class SystemName(str):
    
    @property
    def is_windows(self):
        if self == 'windows':
            return True
        if self.startswith('msys'):
            return True
        return False
    
    @property
    def is_linux(self):
        return self == 'linux'

class _VendorToolchainTag:
    pass

class Toolchain(BaseToolchain, Logging):

    kind = 'cxx'
    languages = [
        Lang('c++', ['c++', 'cxx', 'cpp', 'cc']),
        Lang('c', ['c']),
        Lang('asm', ['s', 'S']),
    ]
    vendors: list[str] = None
    vendor: str = None
    data: dict = None
    tools: dict = None
    SettingsClass = ToolchainSettings

    @classmethod
    def load(cls):
        from dan.cxx.detect import get_toolchains
        import dan.cxx.unix_toolchain
        import dan.cxx.msvc_toolchain

        toolchains = []
        toolchains_data = get_toolchains(False)
        toolchain_classes = list(Toolchain.registered_classes(Toolchain))
        for tc_name, tc_data in toolchains_data['toolchains'].items():
            tc_type = tc_data['type']
            for cls in toolchain_classes:
                if tc_type in cls.vendors:
                    class VendorToolchain(cls):
                        final = True
                        vendor = tc_type
                        name = tc_name
                        data = tc_data
                        tools = toolchains_data['tools']
                    toolchains.append(VendorToolchain)
        return toolchains



    def __init__(self, settings: ToolchainSettings = None, cache: dict = None) -> None:
        super().__init__(settings or ToolchainSettings())
        self.cc : Path = None
        self.cxx : Path = None
        self._compile_commands: CompileCommands = None
        self.cxx_flags = list()
        self.type = self.data['type']
        self.arch = self.data['arch']
        self.env = self.data['env']
        self.system = SystemName(self.data['system'])
        self.version = Version(self.data['version'])
        self.cache = dict() if cache is None else cache
        self.get_logger(f'{self.type}-{self.version}')
        self.env = None
        self.rpath = None
        self.compile_options: list[str] = list()
        self.link_options: list[str] = list()
        self.rpath = None
        self.runtime = RuntimeType.dynamic

    # @property
    # def arch(self):
    #     self.__update_cache()
    #     return self.cache['arch']
    
    @property
    def is_host(self):
        self.__update_cache()
        return self.cache['is_host']
    
    @property
    def up_to_date(self):
        if not 'arch' in self.cache or self.cache['arch'] is None:
            return False
        if not 'arch_detect_flags' in self.cache or self.cache['arch_detect_flags'] != self.settings.compile_flags:
            return False
        return True
    
    @property
    def build_type(self):
        return self.settings.build_type
    
    def __update_cache(self):
        if self.up_to_date:
            return

        from dan.cxx.detect import get_compiler_defines, get_target_arch
        defines = get_compiler_defines(self.cc, self.type, self.settings.compile_flags, self.env)
        arch = get_target_arch(defines)
        self.cache['defines'] = defines
        self.cache['arch'] = arch
        self.cache['arch_detect_flags'] = self.settings.compile_flags
        
        from dan.core.osinfo import OSInfo
        osi = OSInfo()
        osi.name = SystemName(osi.name)
        is_host = False
        if self.arch == osi.arch:
            if self.system == osi.name or self.system.is_windows and osi.name.is_windows:
                is_host = True

        self.cache['is_host'] = is_host

    async def get_default_defines(self) -> dict[str, str]:
        return self.cache['defines']

    # @property
    # def compile_commands(self):
    #     if not self._compile_commands:
    #         self._compile_commands = CompileCommands()
    #     return self._compile_commands

    def init(self):
        self.__update_cache()

    def has_cxx_compile_options(*opts) -> bool:
        raise NotImplementedError()

    def make_compile_definitions(self, definitions: set[str]) -> list[str]:
        raise NotImplementedError()
    
    def make_compile_options(self, options: set[str]) -> list[str]:
        raise NotImplementedError()

    def make_include_options(self, include_paths: set[Path]) -> list[str]:
        raise NotImplementedError()

    def make_libpath_options(self, libraries: set[Path | str]) -> list[str]:
        raise NotImplementedError()

    def make_link_options(self, libraries: set[Path]) -> list[str]:
        raise NotImplementedError()

    def make_library_name(self, basename: str, shared: bool) -> str:
        raise NotImplementedError()

    def make_executable_name(self, basename: str) -> str:
        raise NotImplementedError()

    async def _handle_compile_output(self, lines) -> t.Iterable[diag.Diagnostic]:
        raise NotImplementedError()
    
    async def _handle_link_output(self, lines) -> t.Iterable[diag.Diagnostic]:
        raise NotImplementedError()

    async def scan_dependencies(self, sourcefile: Path, output: Path, options: set[str]) -> set[FileDependency]:
        raise NotImplementedError()

    def compile_generated_files(self, output: Path) -> set[Path]:
        return set()
    
    def debug_files(self, output: Path) -> set[Path]:
        return set()

    def make_compile_commands(self, sourcefile: Path, output: Path, options: list[str], build_type=None) -> CommandArgsList:
        raise NotImplementedError()
    
    def from_unix_flags(self, flags: list[str]) -> list[str]:
        """Convert flags from unix-style to target-compiler-style"""
        return flags
    
    def to_unix_flags(self, flags: list[str]) -> list[str]:
        """Convert flags from target-compiler-style to unix-style"""
        return flags

    async def compile(self, sourcefile: Path, output: Path, options: list[str], cwd: Path, build_type=None, **kwds):
        commands = self.make_compile_commands(sourcefile, output, options, build_type)
        diags = []
        if diag.enabled:
            async def capture(stream):
                with stream as lines:
                    async for diag in self._handle_compile_output(lines):
                        diags.append(diag)
            kwds['all_capture'] = capture
        for index, command in enumerate(commands):
            try:
                await self.run(f'compile{index}', output, command, **kwds, cwd=cwd)
            except CommandError as err:
                raise CompilationFailure(err, sourcefile, options, command, self, diags) from None
        return commands, diags

    def make_link_commands(self, objects: set[Path], output: Path, options: list[str]) -> CommandArgsList:
        raise NotImplementedError()

    async def link(self, objects: set[Path], output: Path, options: list[str], cwd: Path, **kwds):
        commands = self.make_link_commands(objects, output, options)
        diags = []
        if diag.enabled:
            async def capture(stream):
                with stream as lines:
                    async for diag in self._handle_link_output(lines):
                        diags.append(diag)
            kwds['all_capture'] = capture
        for index, command in enumerate(commands):
            try:
                await self.run(f'link{index}', output, command, **kwds, cwd=cwd)
            except CommandError as err:
                raise LinkageFailure(err, objects, options, command, self, diags) from None
        return commands, diags

    def make_static_lib_commands(self, objects: set[Path], output: Path, options: set[str]) -> CommandArgsList:
        raise NotImplementedError()

    async def static_lib(self, objects: set[Path], output: Path, options: set[str], cwd: Path, **kwds):
        commands = self.make_static_lib_commands(objects, output, options)
        for index, command in enumerate(commands):
            try:
                await self.run(f'static_lib{index}', output, command, **kwds, cwd=cwd)
            except CommandError as err:
                raise LinkageFailure(err, objects, options, command, self) from None
        return commands

    def make_shared_lib_commands(self, objects: set[Path], output: Path, options: set[str]) -> tuple[Path, CommandArgsList]:
        raise NotImplementedError()

    async def shared_lib(self, objects: set[Path], output: Path, options: set[str], cwd: Path, **kwds):
        commands = self.make_shared_lib_commands(objects, output, options)
        for index, command in enumerate(commands):
            await self.run(f'shared_lib{index}', output, command, **kwds, cwd=cwd)
        return commands

    async def run(self, name: str, output: Path, args, quiet=False, **kwds) -> tuple[str, str, int]:
        return await async_run(args, env={**(self.env or dict()), 'LC_ALL': 'C'}, logger=self if not quiet else None, **kwds)

    @property
    def cxxmodules_flags(self) -> list[str]:
        ...

    def can_compile(self, source: str, options: list[str] = list(), extension='.cpp'):
        with tempfile.NamedTemporaryFile('w', suffix=extension) as f:
            f.write(source)
            f.flush()
            fname = Path(f.name)
            _, __, rc = sync_run(self.make_compile_commands(fname, fname.with_suffix('.o'), options)[0], no_raise=True)
            return rc == 0

    def has_include(self, *includes, options: list[str] = list(), extension='.cpp'):
        source = '\n'.join([f'#include {inc}' for inc in includes])
        return self.can_compile(source, options, extension)

    def has_definition(self, *definitions, options: list[str] = list(), extension='.cpp'):
        source = '\n'.join([f'''#ifndef {d}
        #error "{d} is not defined"
        #endif''' for d in definitions])
        return self.can_compile(source, options, extension)
    
    async def get_default_include_paths(self, lang = 'c++') -> list[str]:
        return []
