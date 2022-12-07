from pymake.core.logging import Logging
from pymake.core.utils import AsyncRunner
from pymake.cxx.toolchain import Toolchain, Path, FileDependency


class GCCToolchain(Toolchain, AsyncRunner, Logging):
    def __init__(self, cc: Path = 'gcc', cxx: Path = 'g++'):
        super().__init__('gcc-toolchain')
        self.cc = cc
        self.cxx = cxx
        self.ar = f'{cc}-ar'
        self.ranlib = f'{cc}-ranlib'

    def make_include_options(self, include_paths: set[Path]) -> set[str]:
        return {f'-I{p}' for p in include_paths}
    
    def make_link_options(self, libraries: set[Path]) -> set[str]:
        opts = set()
        opts.update([f'-L{p.parent}' for p in libraries])
        opts.update([f'-Wl,-rpath,{p.parent}' for p in libraries])
        opts.update([f'-l{p.stem.removeprefix("lib")}' for p in libraries])
        return opts

    async def scan_dependencies(self, file: Path, options: set[str]) -> set[FileDependency]:
        out, _, _ = await self.run(f'{self.cxx} -M {file} {" ".join(options)}')
        all = ''.join([dep.replace('\\', ' ')
                      for dep in out.splitlines()]).split()
        _obj = all.pop(0)
        _src = all.pop(0)
        return {FileDependency(dep) for dep in all}

    def compile_generated_files(self, output: Path) -> set[Path]:
        return {output.with_suffix(output.suffix + '.d')}

    @property
    def cxxmodules_flags(self) -> set[str]:
        return {'-std=c++20', '-fmodules-ts'}

    async def compile(self, sourcefile: Path, output: Path, options: set[str]):
        await self.run(f'{self.cxx} {" ".join(options)} -MD -MT {output} -MF {output}.d -o {output} -c {sourcefile}')

    async def link(self, objects: set[Path], output: Path, options: set[str]):
        await self.run(f'{self.cxx} {" ".join(objects)} -o {output} {" ".join(options)}')

    async def static_lib(self, objects: set[Path], output: Path, options: set[str] = set()):
        await self.run(f'{self.ar} qc {output} {" ".join(options)} {" ".join(objects)}')
        await self.run(f'{self.ranlib} {output}')
    
    async def shared_lib(self, objects: set[Path], output: Path, options: set[str] = set()):
        await self.run(f'{self.cxx} -shared {" ".join(options)} {" ".join(objects)} -o {output}')
