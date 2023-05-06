from typing import Iterable
from pymake.core.pathlib import Path
from pymake.core import aiofiles
from pymake.core.target import Target
from pymake.core.runners import async_run


class GitSources(Target, internal=True):

    url: str = None
    refspec: str = None
    patches: Iterable = list()

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.sha1 = None
        self.output: Path = self.build_path / 'sources'
        self.git_dir: Path = self.output / '.git'

    async def __build__(self):
        try:
            self.output.mkdir()
            await async_run(f'git init', logger=self, cwd=self.output)
            await async_run(f'git config advice.detachedHead off', logger=self, cwd=self.output)
            await async_run(f'git remote add origin {self.url}', logger=self, cwd=self.output)
            await async_run(f'git fetch -q --depth 1 origin {self.refspec}', logger=self, cwd=self.output)
            await async_run(f'git checkout FETCH_HEAD', logger=self, cwd=self.output)
            
            for patch in self.patches:
                await async_run(f'git am {self.source_path / patch}', logger=self, cwd=self.output)

        except Exception as e:
            await aiofiles.rmtree(self.output)
            raise e

