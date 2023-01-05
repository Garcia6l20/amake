import asyncio
from pymake.core import aiofiles
from pymake.core.pathlib import Path
from pymake.logging import Logging


class AsyncExecutable(Logging):
    async def execute(self, *args, **kwargs): ...


class Test:
    def __init__(self, makefile,
                 executable: AsyncExecutable,
                 name: str = None,
                 args:list[str] = list(),
                 file: Path | str = None,
                 lineno: int = None,
                 workingDir: Path = None):
        self.name = name or executable.name
        self.fullname = f'{makefile.fullname}.{self.name}'
        self.executable = executable
        self.file = Path(file) if file else None
        self.lineno = lineno
        self.workingDir = workingDir or makefile.build_path
        self.args = args

    async def __call__(self):
        out, err, rc = await self.executable.execute(*self.args, no_raise=True)
        async with aiofiles.open(self.workingDir / f'{self.name}.stdout', 'w') as outlog, \
              aiofiles.open(self.workingDir / f'{self.name}.stderr', 'w') as errlog:
              await asyncio.gather(outlog.write(out), errlog.write(err))
        if rc != 0:
            self.executable.error(
                f'Test \'{self.name}\' failed !\nstdout: {out}\nstderr: {err}')
            return False
        else:
            self.executable.info(f'Test \'{self.name}\' succeed !')
            return True
