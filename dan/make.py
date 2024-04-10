from dataclasses import dataclass, field
import functools
import itertools
import os
import fnmatch

from dataclasses_json import dataclass_json
import sys

from dan import logging

import dan.core.typing as t
from dan.core import diagnostics as diag
from dan.core.cache import Cache
from dan.core.makefile import MakeFile
from dan.core.pathlib import Path
from dan.core.include import MakeFileError, include_makefile, Context
from dan.core import aiofiles, asyncio
from dan.core.requirements import RequiredPackage, load_requirements
from dan.core.settings import InstallMode, InstallSettings, BuildSettings
from dan.core.test import Test
from dan.core.utils import IndexList, unique
from dan.cxx import init_toolchain
from dan.core.target import Option, Target
from dan.cxx.targets import Executable
from dan.core.runners import max_jobs
from dan.core.terminal import TerminalMode, TermStream, set_mode as set_terminal_mode
from dan.core.utils import Environment, flatten


@dataclass_json
@dataclass
class Config:
    source_path: Path = None
    build_path: Path = None
    current_context: str = None
    settings: dict[str, BuildSettings] = field(default_factory=lambda: dict())


class ConfigCache(Cache[Config]):
    indent = 4


def _get_code_position(code, instruction_index):
    if instruction_index < 0 or not hasattr(
        code, "co_positions"
    ):  # Python <= 3.10 does not have co_position
        return (None, 0, 0, 0)
    positions_gen = code.co_positions()
    return next(itertools.islice(positions_gen, instruction_index // 2, None))


def _tb_entries(tb):
    entries = list()
    while tb is not None:
        entries.append(tb)
        tb = tb.tb_next
    return entries


def _walk_tb(tb, reverse=True):
    if isinstance(tb, Exception):
        tb = tb.__traceback__

    entries = _tb_entries(tb)

    if reverse:
        entries = reversed(entries)

    for tb in entries:
        positions = _get_code_position(tb.tb_frame.f_code, tb.tb_lasti)
        # Yield tb_lineno when co_positions does not have a line number to
        # maintain behavior with walk_tb.
        if positions[0] is None:
            yield tb.tb_frame.f_code.co_filename, (tb.tb_lineno,) + positions[1:]
        else:
            yield tb.tb_frame.f_code.co_filename, positions


def gen_python_diags(err: Exception):
    diagnostics = diag.DiagnosticCollection()
    if not diag.enabled:
        return diagnostics
    match err:
        case MakeFileError():
            cause = err.__cause__
            related = None
            if isinstance(cause, MakeFileError):
                related = list()
                cause_diags = gen_python_diags(cause)
                for filename, diagns in cause_diags.items():
                    for d in diagns:
                        related.append(
                            diag.RelatedInformation(
                                diag.Location(diag.Uri(filename), d.range), d.message
                            )
                        )
                diagnostics.update(cause_diags)
                err_filename = str(err.path)
                for filename, (lineno, end_lineno, colno, end_colno) in _walk_tb(cause):
                    if filename == err_filename:
                        break
            else:
                for filename, (lineno, end_lineno, colno, end_colno) in _walk_tb(cause):
                    break
            diagnostics[filename] = diag.Diagnostic(
                message=str(cause),
                range=diag.Range(
                    start=diag.Position(lineno - 1, colno),
                    end=diag.Position(end_lineno - 1, end_colno),
                ),
                code=cause.__class__.__name__,
                source="dan.make",
                related_information=related,
            )
        case _:
            for filename, (lineno, end_lineno, colno, end_colno) in _walk_tb(err):
                break
            diagnostics[filename] = diag.Diagnostic(
                message=str(err),
                range=diag.Range(
                    start=diag.Position(lineno - 1, colno),
                    end=diag.Position(end_lineno - 1, end_colno),
                ),
                code=err.__class__.__name__,
                source="dan.make",
            )

    return diagnostics


class Make(logging.Logging):
    _config_name = "dan.config.json"
    _cache_name = "dan.cache"

    def __init__(
        self,
        build_path: str,
        source_path: str = None,
        targets: list[str] = None,
        verbose: int = 0,
        for_install: bool = False,
        jobs: int = None,
        terminal_mode: TerminalMode = None,
        diags=False,
        contexts: list[str] = None,
        all=False,
        quiet=False,
    ):
        jobs = jobs or os.cpu_count()
        max_jobs(jobs)

        if quiet:
            verbose = -1

        match verbose:
            case 1:
                log_level = logging.DEBUG
            case 2:
                log_level = logging.TRACE
            case -1:
                log_level = logging.ERROR
            case 0:
                log_level = logging.INFO
            case _:
                logging.getLogger().warning(
                    "unknown verbosity level: %s, using INFO", verbose
                )
                log_level = logging.INFO

        logging.getLogger().setLevel(log_level)

        if terminal_mode is not None:
            set_terminal_mode(terminal_mode)

        self.term = TermStream("make")

        if diags:
            diag.enabled = True

        self.for_install = for_install

        self.build_path = Path(build_path)
        self.config_path = build_path / self._config_name
        self.cache_path = build_path / self._cache_name

        self.required_targets = targets
        sys.pycache_prefix = str(build_path / "__pycache__")
        self._config = ConfigCache.instance(self.config_path)
        self.cache = Cache.instance(self.cache_path, binary=True)

        if self.config.build_path is None:
            self.config.build_path = str(self.build_path)

        if source_path is not None:
            self.config.source_path = str(source_path)

        self.debug(f"jobs: {jobs}")

        self._diagnostics = diag.DiagnosticCollection()

        if all:
            contexts = self.config.settings.keys()
        elif not contexts:
            contexts = [self.config.current_context]

        self.contexts: IndexList[Context] = IndexList()
        for context_name in contexts:
            if context_name in self.config.settings:
                self.contexts.append(
                    Context(context_name, self.config.settings[context_name])
                )

    def context(self, name: str = None) -> Context:
        if name is None:
            assert len(self.contexts) == 1, "context must be specified"
            return self.contexts[0]
        return self.contexts[name]

    @property
    def config(self) -> Config:
        return self._config.data

    @property
    def settings(self) -> BuildSettings:
        return self._config.data.settings

    @property
    def source_path(self):
        return Path(self.config.source_path)

    @property
    def toolchain(self):
        assert len(self.contexts) == 1, "Only available in single-context mode"
        return self.contexts[0].get("cxx_toolchain")

    @property
    def env(self) -> dict[str, str]:
        from dan.cxx.detect import get_dan_path

        env = Environment(self.toolchain.env)
        paths = [str(get_dan_path() / "os-utils" / "bin")]
        for t in self.executable_targets:
            parts = t.env.get("PATH", os.environ["PATH"]).split(os.pathsep)
            paths.extend(parts)
        env.path_prepend(*paths)
        return env

    async def project_config(self):
        from dan.core.config import load_project_config, ConfigContext

        project_config = load_project_config(self.source_path)

        from dan.core.toolchains import BaseToolchain

        toolchain_classes = BaseToolchain.load_all()

        # fake_cache = dict()
        # toolchains = []
        # for toolchain_class in BaseToolchain.registered_classes(lambda t: t.final):
        #     toolchains.append(toolchain_class())

        contexts = []
        for ctx_class in ConfigContext.registered_classes():
            ctx = ctx_class()
            for ToolchainClass in toolchain_classes:
                settings = ToolchainClass.SettingsClass()
                toolchain = ToolchainClass()
                if ctx.try_configure(toolchain, settings):
                    ctx.accepted_toolchains.append((toolchain, settings))
            contexts.append(ctx)

        return contexts

    async def configure(self, context: str = None, toolchain: str = None):
        self.info("source path: %s", self.config.source_path)
        self.info("build path: %s", self.config.build_path)
        if not context in self.config.settings:
            self.config.settings[context] = BuildSettings()
        settings = self.config.settings[context]
        if toolchain:
            settings.toolchain = toolchain
        if not settings.toolchain:
            self.warning("no toolchain configured")
        if not self.config.current_context:
            self.config.current_context = context
            self.info("setting current context to %s", context)
        if context not in self.contexts:
            if settings.config is None:
                from dan.core.toolchains import BaseToolchain

                toolchain_classes = BaseToolchain.load_all()
                for ToolchainClass in toolchain_classes:
                    if ToolchainClass.name == settings.toolchain:
                        settings.config = ToolchainClass.SettingsClass()
                        break
            self.contexts.append(Context(context, self.config.settings[context]))
        await self._config.save()

    @asyncio.cached
    async def initialize(self):
        assert self.config_path.exists(), "configure first"

        self.debug(f"source path: {self.source_path}")
        self.debug(f"build path: {self.build_path}")

        for ctx in self.contexts:

            toolchain = init_toolchain(ctx)

            self.info(
                f"{ctx.name}: using '{toolchain.name}' toolchain in '{toolchain.build_type.name}' mode"
            )

            with ctx:
                try:
                    include_makefile(self.source_path, self.build_path / ctx.name)
                except MakeFileError as err:
                    self._diagnostics.update(gen_python_diags(err))
                    raise

    def __matches(self, target: Target | Test, ctx: Context):
        for required in self.required_targets:
            if "/" in required:
                ctx_name, required = required.split("/")
            else:
                ctx_name = None
            if ctx_name and ctx_name != ctx.name:
                return False
            if fnmatch.fnmatch(target.fullname, f"*{required}*"):
                return True
        return False

    def makefiles(self, context=None) -> list[Target]:
        items = set()
        contexts = [self.context(context)] if context is not None else self.contexts
        for ctx in contexts:
            items.add(ctx.root)
            items.update(ctx.all_makefiles)
        return list(items)

    def targets(self, context=None) -> list[Target]:
        items = list()
        contexts = [self.context(context)] if context is not None else self.contexts
        for ctx in contexts:
            if self.required_targets and len(self.required_targets) > 0:
                for target in ctx.root.all_targets:
                    if self.__matches(target, ctx):
                        items.append(target)
            else:
                items.extend(ctx.root.all_default)
        return items

    @functools.cached_property
    def tests(self) -> list[Test]:
        items = list()
        if self.required_targets and len(self.required_targets) > 0:
            for required in self.required_targets:
                pos = required.find(":")
                if pos > 0:
                    test_name = required[:pos]
                    test_case = required[pos + 1 :]
                else:
                    test_name = required
                    test_case = None

                for ctx in self.contexts:
                    for test in ctx.root.all_tests:
                        if fnmatch.fnmatch(test.fullname, f"*{test_name}*"):
                            if len(test) > 1 and test_case is not None:
                                cases = list()
                                for case in test.cases:
                                    if fnmatch.fnmatch(case.name, test_case):
                                        cases.append(case)
                                if len(cases) == 0:
                                    self.warning(
                                        "couldn't find any test case in %s matching '%s'",
                                        test.name,
                                        test_case,
                                    )
                                test.cases = cases

                            items.append(test)
        else:
            for ctx in self.contexts:
                for test in ctx.root.all_tests:
                    items.append(test)
        return items

    def all_options(self) -> list[Option]:
        opts = []
        for target in self.targets():
            for o in target.options:
                opts.append(o)

        for ctx in self.contexts:
            for makefile in ctx.all_makefiles:
                for o in makefile.options:
                    opts.append(o)
        return opts

    @property
    def diagnostics(self):
        result: diag.DiagnosticCollection = diag.DiagnosticCollection()
        for ctx in self.contexts:
            if ctx.root:
                for target in ctx.root.all_targets:
                    result.update(target.diagnostics)
        result.update(self._diagnostics)
        return result

    async def targets_of(self, sources: list[Path]) -> dict[Path, Target | None]:
        from dan.cxx.targets import CXXObjectsTarget

        result = {Path(source): None for source in sources}

        def check(t: CXXObjectsTarget):
            nonlocal result
            for source in result.keys():
                if t.source_path not in source.parents:
                    continue
                if source.suffix[1].lower() == "h":
                    for p in [*t.includes.private_raw, *t.includes.public_raw]:
                        p: Path = p
                        if p not in source.parents:
                            continue
                        result[source] = t
                else:
                    t._init_sources()
                    if source.name in [Path(s).name for s in t.sources]:
                        result[source] = t

        object_targets = [
            target
            for target in self.context().root.all_targets
            if isinstance(target, CXXObjectsTarget)
        ]

        def _on_done(task: asyncio.Task):
            nonlocal result
            if not task.cancelled():
                done = None not in result.values()
                if done:
                    g.cancel()

        async with asyncio.TaskGroup() as g:
            for t in object_targets:
                g.create_task(asyncio.async_wait(check, t)).add_done_callback(_on_done)

        return result

    async def apply_options(self, *options):
        await self.initialize()
        from dan.core.settings import _apply_inputs

        all_opts = self.all_options()

        def get_option(name):
            for opt in all_opts:
                if opt.fullname == name:
                    return opt.cache, opt.value, opt.type

        _apply_inputs(options, get_option, logger=self, input_type_name="option")

    async def apply_settings(self, *settings, context=None):
        from dan.core.settings import apply_settings

        contexts = self.contexts if context is None else [self.contexts[context]]
        for ctx in contexts:
            apply_settings(ctx.settings, *settings, logger=self)

    @staticmethod
    def toolchains():
        from dan.cxx.detect import get_toolchains

        return get_toolchains()

    async def _build_target(self, t: Target):
        try:
            await t.build()
        except Exception as err:
            self._diagnostics.update(gen_python_diags(err))
            raise

    async def get_all_dependencies(self, target, deps: set):
        if target in deps:
            return
        await load_requirements(
            target.requires, makefile=target.makefile, logger=self, install=True
        )
        for d in target.dependencies.all:
            match d:
                case RequiredPackage():
                    if d.target:
                        deps.add(d.target)
                        await self.get_all_dependencies(d.target, deps)
                case Target():
                    deps.add(d)
                    await self.get_all_dependencies(d, deps)

    async def build(self, targets: list[Target] = None):

        await self.initialize()

        if targets is None:
            targets = self.targets()

        all_targets = set()
        async with self.term.task_group("installing dependencies...") as g:
            for t in targets:
                g.create_task(self.get_all_dependencies(t, all_targets))

        all_targets.update(targets)

        self.term.status("building...")
        async with self.term.task_group("building...") as g:
            for t in all_targets:
                g.create_task(self._build_target(t))

        self.term.status("done", icon="✔")

    async def _install_target_deps(self, t: Target):
        deps_install_path = self.root.pkgs_path
        deps_settings = InstallSettings(deps_install_path)
        try:
            await load_requirements(
                t.requires, makefile=self.root, logger=self, install=True
            )

        except Exception as err:
            self._diagnostics.update(gen_python_diags(err))
            raise

    async def install_dependencies(self, targets: list[Target] = None):
        await self.initialize()

        if targets is None:
            targets = self.targets

        async with self.term.task_group("installing dependencies...") as g:
            for target in targets:
                g.create_task(self._install_target_deps(target))

    async def _install_target(self, t: Target, mode: InstallMode):
        try:
            return await t.install(self.settings.install, mode)
        except Exception as err:
            self._diagnostics.update(gen_python_diags(err))
            raise

    async def install(self, mode: InstallMode = InstallMode.user):
        await self.initialize()

        self.for_install = True
        await self.initialize()
        targets = []
        for t in self.targets:
            if t.installed:
                targets.append(t)
            else:
                self.debug("skipping %s (not marked as installed)", t.fullname)

        await self.build(targets)

        async with self.term.task_group("installing targets...") as g:
            for target in targets:
                g.create_task(self._install_target(target, mode))

        installed_files = unique(flatten(g.results()))
        manifest_path = (
            self.settings.install.data_destination
            / "dan"
            / f"{self.root.name}-manifest.txt"
        )
        manifest_path.parent.mkdir(parents=True, exist_ok=True)

        async with aiofiles.open(manifest_path, "w") as f:
            await f.writelines(
                [
                    os.path.relpath(p, manifest_path.parent) + "\n"
                    for p in installed_files
                ]
            )

    async def package(self, pkg_type: str, mode: InstallMode = InstallMode.user):
        import tempfile

        targets = self.targets
        if len(targets) == 1:
            base_name = targets[0].name
            version = targets[0].version
        else:
            base_name = self.root.name

        if not version:
            version = self.root.version

        if version:
            base_name += f"-v{version}"

        archive_name = f"{base_name}.{pkg_type}"

        with tempfile.TemporaryDirectory(prefix=archive_name) as tmp_dir:
            self.settings.install.destination = tmp_dir
            tmp_dir = Path(tmp_dir)
            if pkg_type.startswith("tar"):
                import tarfile

                comp_type = pkg_type.split(".")[1]
                self.info("creating %s", archive_name)
                await self.install(mode)
                with tarfile.open(archive_name, f"w:{comp_type}") as archive:
                    for it in tmp_dir.iterdir():
                        archive.add(it, arcname=it.relative_to(tmp_dir))
            elif pkg_type == "zip":
                import zipfile

                self.info("creating %s", archive_name)
                await self.install(mode)
                with zipfile.ZipFile(archive_name, "w") as archive:
                    for folder_name, sub_folders, file_names in os.walk(tmp_dir):
                        for filename in file_names:
                            file_path = Path(folder_name) / filename
                            archive.write(file_path, file_path.relative_to(tmp_dir))
            else:
                raise RuntimeError(f"Unhandled package type: {pkg_type}")

    @property
    def executable_targets(self) -> list[Executable]:
        return [exe for exe in self.targets() if isinstance(exe, Executable)]

    async def scan_toolchains(self, script: Path = None):
        from dan.cxx.detect import create_toolchains, load_env_toolchain

        if script:
            load_env_toolchain(script)
        else:
            create_toolchains()

    async def run(self):
        await self.initialize()
        results = await asyncio.gather(
            *[t.execute(log=True) for t in self.executable_targets]
        )
        for result in results:
            if result[2] != 0:
                return result[2]
        return 0

    async def _test_target(self, t: Test):
        try:
            return await t.run_test()
        except Exception as err:
            self._diagnostics.update(gen_python_diags(err))
            raise

    async def test(self):
        await self.initialize()
        tests = self.tests
        if len(tests) == 0:
            self.error("No test selected")
            return 255

        async with self.term.task_group("testing...") as g:
            for test in tests:
                g.create_task(self._test_target(test))

        if all(g.results()):
            self.info("Success !")
            return 0
        else:
            self.error("Failed !")
            return 255

    async def clean(self):
        await self.initialize()
        await asyncio.gather(*[t.clean() for t in self.targets()])
