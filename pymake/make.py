
import functools
import logging
from pymake.core.pathlib import Path
import sys
from tqdm import tqdm

from pymake.core.cache import Cache
from pymake.core.include import include_makefile
from pymake.core import aiofiles, asyncio
from pymake.cxx import init_toolchains
from pymake.logging import Logging
from pymake.core.target import Option, Target
from pymake.cxx.targets import Executable


def make_target_name(name: str):
    return name.replace('_', '-')


class Make(Logging):
    _config_name = 'pymake.config.yaml'
    _cache_name = 'pymake.cache.yaml'

    def __init__(self, path: str, targets: list[str] = None, verbose: bool = False, quiet: bool = False):

        from pymake.core.include import context_reset
        context_reset()

        if quiet:
            assert not verbose, "'quiet' cannot be combined with 'verbose'"
            log_level = logging.ERROR
        elif verbose:
            log_level = logging.DEBUG
        else:
            log_level = logging.INFO
        logging.getLogger().setLevel(log_level)

        super().__init__('make')

        self.config = None
        self.cache = None
        path = Path(path)
        if not path.exists() or not (path / 'makefile.py').exists():
            self.source_path = Path.cwd().absolute()
            self.build_path = path.absolute().resolve()
        else:
            self.source_path = path.absolute().resolve()
            self.build_path = Path.cwd().absolute()

        self.config_path = self.build_path / self._config_name
        self.cache_path = self.build_path / self._cache_name

        self.required_targets = targets
        self.build_path.mkdir(exist_ok=True, parents=True)
        sys.pycache_prefix = str(self.build_path / '__pycache__')
        self.config = Cache(self.config_path)
        self.cache = Cache(self.cache_path)

        self.source_path = Path(self.config.get('source_path', self.source_path))

        self.debug(f'source path: {self.source_path}')
        self.debug(f'build path: {self.build_path}')
        
        assert (self.source_path /
                'makefile.py').exists(), f'no makefile in {self.source_path}'
        assert (self.source_path !=
                self.build_path), f'in-source build are not allowed'

    def configure(self, toolchain, build_type):
        self.config.source_path = str(self.source_path)
        self.config.build_path = str(self.build_path)
        self.config.toolchain = toolchain
        self.config.build_type = build_type

    @asyncio.once_method
    async def initialize(self):
        assert self.source_path and self.config_path.exists(), 'configure first'

        toolchain = self.config.toolchain
        build_type = self.config.build_type
        init_toolchains(toolchain)
        self.info(f'using \'{toolchain}\' in \'{build_type}\' mode')
        include_makefile(self.source_path, self.build_path)

        from pymake.core.include import context
        from pymake.cxx import target_toolchain
        target_toolchain.set_mode(build_type)

        self.active_targets: dict[str, Target] = dict()

        if self.required_targets and len(self.required_targets) > 0:
            for target in context.all_targets:
                if target.name in self.required_targets or target.fullname in self.required_targets:
                    self.active_targets[target.fullname] = target
        else:
            for target in context.default_targets:
                self.active_targets[target.fullname] = target

        self.debug(f'targets: {[name for name in self.active_targets.keys()]}')

    @staticmethod
    def all_options() -> list[Option]:
        from pymake.core.include import context
        opts = []
        for target in context.all_targets:
            for o in target.options:
                opts.append(o)
        for makefile in context.all_makefiles:
            for o in makefile.options:
                opts.append(o)
        return opts

    @property
    def toolchains(self):
        from pymake.cxx.detect import get_toolchains
        return get_toolchains()

    async def build(self):
        await self.initialize()
        targets = set(self.active_targets.values())
        pbar = tqdm(total=len(targets), desc='building')
        import shutil
        term_cols = shutil.get_terminal_size().columns
        max_desc_width = int(term_cols * 0.25)

        tsks = list()
        def set_desc():
            desc = 'building ' + ', '.join([t.name for t in targets])
            if len(desc) > max_desc_width:
                desc = desc[:max_desc_width] + ' ...'
            pbar.set_description_str(desc)

        def on_done(t: Target, *args, **kwargs):
            targets.remove(t)
            set_desc()
            pbar.update()

        for t in targets:
            tsk = asyncio.create_task(t.build())
            tsk.add_done_callback(functools.partial(on_done, t))
            tsks.append(tsk)

        set_desc()

        await asyncio.gather(*tsks)

    async def install(self, destination : Path):
        from pymake.core.include import context
        
        await self.initialize()

        targets = dict()
        for target in context.installed_targets:
            if target.fullname in self.active_targets.keys():
                targets[target.fullname] = target

        self.active_targets = targets

        await self.build()

        tasks = []
        for target in targets.values():
            if isinstance(target, Executable):
                dest = destination / 'bin' / target.output.name
            else:
                raise NotImplementedError(f'installation of {type(target)} is not implemented yet !')
            if dest.exists() and dest.younger_than(target.output):
                self.info(f'{dest} is up-to-date')
            else:
                self.info(f'installing {target.fullname} to {dest}')            
                dest.parent.mkdir(parents=True, exist_ok=True)
                tasks.append(aiofiles.copy(target.output, dest))
        await asyncio.gather(*tasks)

    @property
    def executable_targets(self) -> list[Executable]:
        return [exe for exe in self.active_targets.values() if isinstance(exe, Executable)]

    async def scan_toolchains(self, script: Path = None):
        from pymake.cxx.detect import create_toolchains, load_env_toolchain
        if script:
            load_env_toolchain(script)
        else:
            create_toolchains()

    async def run(self):
        await self.initialize()
        await asyncio.gather(*[t.execute() for t in self.executable_targets])

    async def clean(self, target: str = None):
        await self.initialize()
        from pymake.cxx import toolchain
        toolchain.scan = False
        from pymake.core.target import Target
        Target.clean_request = True
        await asyncio.gather(*[t.clean() for t in self.active_targets.values()])
        from pymake.cxx import target_toolchain

        target_toolchain.compile_commands.clear()
