from pathlib import Path

from pymake.core.target import FileDependency


class Toolchain:
    def make_include_options(self, include_paths: list[Path]) -> list[str]:
        ...

    async def scan_dependencies(self, file: Path, options: list[str]) -> list[FileDependency]:
        ...

    async def compile(self, sourcefile: Path, output: Path, options: list[str]):
        ...

    async def link(self, objects: list[Path], output: Path, options: list[str]):
        ...
