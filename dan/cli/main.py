from fnmatch import fnmatch
import os
import sys
import contextlib
import json

from dan import logging

from dan.core.find import find_file
from dan.core.pathlib import Path
from dan.core.terminal import TerminalMode, set_mode as set_terminal_mode, manager as term_manager

from dan.cli import click

from dan.core import diagnostics, asyncio
from dan.core.cache import Cache
from dan.core.settings import BuildSettings
from dan.cxx.targets import Executable


from dan.make import InstallMode, Make
from dan.cli.vscode import Code


_minimal_options = [
    click.option('--build-path', '-B', help='Path where dan has been initialized.',
                 type=click.Path(resolve_path=True, path_type=Path), required=True, default='build', envvar='DAN_BUILD_PATH'),
]

_common_opts = [
    *_minimal_options,
    click.option('--quiet', '-q', is_flag=True,
                 help='Dont print informations (errors only).', envvar='DAN_QUIET'),
    click.option('--verbose', '-v', count=True,
                 help='Verbosity level.', envvar='DAN_VERBOSE'),
    click.option('--jobs', '-j',
                 help='Maximum jobs.', default=None, type=int, envvar='DAN_JOBS'),
    click.option('--no-status', is_flag=True,
                 help='Disable status', envvar='DAN_NOSTATUS'),
    click.option('--all', '-a', is_flag=True,
                help='Use all contexts'),
    click.option('--context', '-c', 'contexts', type=click.ContextParamType(), multiple=True,
                help='Use this context'),
]


def add_options(options):
    def _add_options(func):
        for option in reversed(options):
            func = option(func)
        return func
    return _add_options


common_opts = add_options(_common_opts)
minimal_options = add_options(_minimal_options)


class CommandsContext:
    def __init__(self, *args, **kwds) -> None:
        self._make_args = [*args]
        self._make_kwds = {**kwds}
        self._make = None

    def update(self, *args, **kwds):
        if len(args):
            self._make_args.extend(*args)
        self._make_kwds.update(**kwds)
    
    @contextlib.asynccontextmanager
    async def __call__(self, *args, quiet=None, no_status=False, no_init=False, code=False, context=None, **kwargs):
        if no_status:
            kwargs['terminal_mode'] = TerminalMode.BASIC
        elif code:
            kwargs['terminal_mode'] = TerminalMode.CODE
        if context:
            kwargs['contexts'] = [context]
        self.update(*args, **kwargs)
        if self._make_kwds.pop('quiet', False) or quiet:
            self._make_kwds['verbose'] = -1
        if self._make is None:
            self._make = Make(*self._make_args, **self._make_kwds)
            if not no_init:
                await self._make.initialize()
        yield self._make

    async def __aexit__(self, *exc):
        pass

pass_context = click.make_pass_decorator(CommandsContext)

@pass_context
def show_diags(ctx: CommandsContext):
    if diagnostics.enabled:
        diags = ctx._make.diagnostics
        if diags:
            click.echo(f'DIAGNOSTICS: {diags.to_json()}')

@click.group(no_args_is_help=True)
@click.version_option(package_name='dan-build')
@click.option('--quiet', '-q', is_flag=True,
              help='Dont print informations (errors only)')
@click.option('--verbose', '-v', count=True,
              help='Verbosity level')
@click.option('--jobs', '-j',
              help='Maximum jobs', default=None, type=int)
@click.pass_context
def cli(ctx: click.AsyncContext, **kwds):
    ctx.obj = CommandsContext(**kwds)
    ctx.call_on_close(show_diags)

@cli.command('cli', context_settings=dict(
    ignore_unknown_options=True,
    allow_extra_args=True,
))
@common_opts
@click.option('--help', is_flag=True)
@pass_context
@click.pass_context
async def user_cli_command(click_ctx, ctx, help, *args, **kwargs):
    """User commands"""
    async with ctx(no_init=False, **kwargs) as make:
        if not click_ctx.args:
            click.echo(user_cli.get_help(click_ctx))
            return 1
        args = click_ctx.args
        if help:
            args.append('--help')
        name, command, args = user_cli.resolve_command(click_ctx, click_ctx.args)
        setattr(click_ctx, 'obj', make)
        cmd_ctx = command.make_context(name, args, parent=click_ctx)
        return asyncio.may_await(cmd_ctx.invoke(command))
user_cli_command.add_help_option = False

@click.group()
def user_cli():
    pass
user_cli.context_class = click.AsyncContext

@cli.command()
@click.argument('context', default='auto', type=click.ContextParamType())
@click.option('--verbose', '-v', count=True,
              help='Verbosity level')
@click.option('--toolchain', '-t', 'in_toolchain', help='The toolchain to use',
              type=click.ToolchainParamType(), envvar='DAN_TOOLCHAIN')
@click.option('--yes', '-y', help='Say yes to all prompts (use defaults)', is_flag=True)
@click.option('--setting', '-s', 'settings', help='Set or change a setting', multiple=True, type=click.SettingsParamType(BuildSettings))
@click.option('--option', '-o', 'options', help='Set or change an option', multiple=True, type=click.OptionsParamType())
@click.option('--build-path', '-B', help='Path where dan has been initialized.',
              type=click.Path(resolve_path=True, path_type=Path), required=True, default='build', envvar='DAN_BUILD_PATH')
@click.option('--source-path', '-S', help='Path where source is located.',
              type=click.Path(resolve_path=True, path_type=Path), required=True, default='.', envvar='DAN_SOURCE_PATH')
@pass_context
async def configure(ctx: CommandsContext, context: str, in_toolchain: str, yes: bool, settings: tuple[str], options: tuple[str], **kwds):
    """Configure dan project"""
    contexts = [context]
    user_contexts = dict()
    async with ctx(no_init=True, no_status=True, contexts=contexts, **kwds) as make:

        if context == 'auto':
            context_config = await make.project_config()
            if len(context_config) == 0:
                context = 'default'
                # make.contexts = [context]
            else:
                contexts.clear()
                for ctx in context_config:
                    assert len(ctx.accepted_toolchains) > 0, f'No suitable toolchain found for {ctx.name} context'
                    contexts.append(ctx.name)
                    user_contexts[ctx.name] = {
                        'toolchains': [(t.name, s) for t, s in ctx.accepted_toolchains],
                    }

        for ctx in contexts:
            toolchain = in_toolchain
            toolchain_settings = None
            
            if ctx in user_contexts and len(user_contexts[ctx]['toolchains']) == 1:
                toolchain = user_contexts[ctx]['toolchains'][0][0]
                toolchain_settings = user_contexts[ctx]['toolchains'][0][1]
            
            if toolchain is None:
                if ctx in make.config.settings:
                    toolchain = make.config.settings[ctx].toolchain

                from dan.cxx.detect import get_toolchains
                if ctx in user_contexts:
                    toolchains = user_contexts[ctx]['toolchains']
                else:
                    toolchains = [*get_toolchains(create=False)["toolchains"].keys(), 'default']
                for default_tc in toolchains:
                    if fnmatch(default_tc, f'*{ctx}*'):
                        break
                if yes:
                    toolchain = default_tc
                else:
                    toolchain = click.prompt('Toolchain', type=click.Choice(toolchains), default=default_tc)

            if ctx in make.config.settings:
                build_settings = make.config.settings[ctx]
            else:
                build_settings = BuildSettings(toolchain=toolchain)
                make.config.settings[ctx] = build_settings

            if toolchain_settings:
                build_settings.config = toolchain_settings
                # user_contexts[ctx]['configure'](build_settings)

            await make.configure(ctx, toolchain)

            if len(settings):
                await make.apply_settings(*settings, context=ctx)

        # NOTE: intializing make after applying setting
        #       to check settings are valid implicitly (cache save skipped)
        await make.initialize()

        if len(options):
            await make.apply_options(*options)


@cli.command()
@click.option('--for-install', is_flag=True, help='Build for install purpose (will update rpaths [posix only])')
@common_opts
@click.option('--force', '-f', is_flag=True,
              help='Clean before building')
@click.argument('TARGETS', nargs=-1, type=click.TargetParamType())
@pass_context
async def build(ctx: CommandsContext, force=False, **kwds):
    """Build targets"""
    async with ctx(**kwds) as make:
        if force:
            await make.clean()
        await make.build()

@cli.command()
@common_opts
@click.option('--force', '-f', is_flag=True,
              help='Force re-install')
@click.argument('TARGETS', nargs=-1, type=click.TargetParamType())
@pass_context
async def install_dependencies(ctx: CommandsContext, force, **kwds):
    """Install build dependencies"""
    async with ctx(**kwds) as make:
        await make.install_dependencies(force=force)

@cli.command()
@common_opts
@click.argument('MODE', type=click.Choice([v.name for v in InstallMode]), default=InstallMode.user.name)
@click.argument('TARGETS', nargs=-1, type=click.TargetParamType())
@pass_context
async def install(ctx: CommandsContext, mode: str, **kwargs):
    """Install targets"""
    async with ctx(**kwargs) as make:
        mode = InstallMode[mode]
        await make.install(mode)


@cli.command()
@common_opts
@click.option('--type', '-t', 'pkg_type', type=click.Choice(['tar.gz', 'zip']), default='tar.gz')
@click.argument('MODE', type=click.Choice([v.name for v in InstallMode]), default=InstallMode.user.name)
@click.argument('TARGETS', nargs=-1, type=click.TargetParamType())
@pass_context
async def package(ctx: CommandsContext, pkg_type, mode: str, **kwargs):
    """Package given targets"""
    async with ctx(**kwargs) as make:
        mode = InstallMode[mode]
        await make.package(pkg_type, mode)


@cli.command()
@click.option('--verbose', '-v', count=True,
              help='Verbosity level')
@click.option('--yes', '-y', is_flag=True, help='Proceed without asking')
@click.option('--root', '-r', help='Root path to search for installation manifest', type=click.Path(exists=True, file_okay=False))
@click.argument('NAME')
def uninstall(verbose: int, yes: bool, root: str, name: str):
    """Uninstall previous installation"""
    if verbose == 0:
        logging.getLogger().setLevel(logging.INFO)
    elif verbose == 1:
        logging.getLogger().setLevel(logging.DEBUG)
    else:
        logging.getLogger().setLevel(logging.TRACE)
    if root:
        paths = [root]
    else:
        paths = [
            '~/.local/share/dan',
            '/usr/local/share/dan',
            '/usr/share/dan',
        ]
    manifest = find_file(f'{name}-manifest.txt$', paths=paths)
    with open(manifest, 'r') as f:
        files = [(manifest.parent / mf.strip()).resolve()
                 for mf in f.readlines()]
    to_be_removed = '\n'.join([f" - {f}" for f in files])
    yes = yes or click.confirm(
        f'Following files will be removed:\n {to_be_removed}\nProceed ?')
    if yes:
        def rm_empty(dir: Path):
            if dir.is_empty:
                click.logger.debug(f'removing empty directory: {dir}')
                os.rmdir(dir)
                rm_empty(dir.parent)

        for f in files:
            click.logger.debug(f'removing: {f}')
            os.remove(f)
            rm_empty(f.parent)

        os.remove(manifest)
        rm_empty(manifest.parent)

@cli.group('set')
@pass_context
def _set(ctx: CommandsContext):
    """Set group"""
    ctx._make_kwds['terminal_mode'] = TerminalMode.BASIC

@_set.command()
@common_opts
@click.argument('CONTEXT', type=click.ContextParamType())
@pass_context
async def context(ctx: CommandsContext, context: str, **kwargs):
    async with ctx(no_init=True, **kwargs) as make:
        context_names = make.config.settings.keys()
        if not context in context_names:
            avail = ', '.join(context_names)
            raise RuntimeError(f'No such context (available: {avail})')
        make.config.current_context = context
        await make.cache.save()


@cli.group('get')
@pass_context
def _get(ctx: CommandsContext):
    """Get group"""
    ctx._make_kwds['terminal_mode'] = TerminalMode.BASIC

@_get.command()
@common_opts
@pass_context
async def context(ctx: CommandsContext, **kwargs):
    """List targets"""
    kwargs['quiet'] = True
    async with ctx(**kwargs) as make:
        click.echo(make.config.current_context)

@_get.command()
@common_opts
@pass_context
async def contexts(ctx: CommandsContext, **kwargs):
    """List targets"""
    kwargs['quiet'] = True
    async with ctx(**kwargs) as make:
        context_names = '\n'.join(make.config.settings.keys())
        click.echo(context_names)


@_get.command()
@click.option('-a', '--all', 'all', is_flag=True, help='Show all targets (not only defaulted ones)')
@click.option('-t', '--type', 'show_type', is_flag=True, help='Show target\'s type')
@common_opts
@click.argument('TARGETS', nargs=-1)
@pass_context
async def targets(ctx: CommandsContext, show_type: bool, all=False, **kwargs):
    """List targets"""
    kwargs['quiet'] = True
    async with ctx(all=all, **kwargs) as make:
        out = []
        for target in make.targets():
            name = target.display_name if all else target.name
            if show_type:
                out.append(name + ' - ' + type(target).__name__)
            else:
                out.append(name)
        click.echo('\n'.join(out))


@_get.command()
@common_opts
@click.argument('TARGETS', nargs=-1)
@pass_context
async def tests(ctx: CommandsContext, **kwargs):
    """List tests"""
    kwargs['quiet'] = True
    async with ctx(**kwargs) as make:
        for t in make.tests:
            if len(t) > 1:
                for c in t.cases:
                    click.echo(f'{t.fullname}:{c.name}')
            else:
                click.echo(t.fullname)

@_get.command()
@common_opts
@click.argument('TARGETS', nargs=-1, type=click.TargetParamType())
@pass_context
async def options(ctx: CommandsContext, **kwargs):
    """List tests"""
    kwargs['quiet'] = True
    async with ctx(**kwargs) as make:
        for o in make.all_options:
            click.echo(f'{o.fullname}: {o.help} (current: {o.value}, default: {o.default}, type: {o.type.__name__})')

@_get.command()
def toolchains(**kwargs):
    """List toolchains"""
    kwargs['quiet'] = True
    for name, _ in Make.toolchains()['toolchains'].items():
        click.echo(name)


@_get.command()
@common_opts
@click.option('-n', '--not-found', help='Show not-found dependencies', is_flag=True)
@click.argument('TARGET', type=click.TargetParamType(target_types=[Executable]))
@pass_context
async def runtime_dependencies(ctx: CommandsContext, not_found, target, **kwargs):
    """Inspect stuff"""
    async with ctx(**kwargs) as make:
        for t in make.root.all_targets:
            if t.fullname == target:
                break
        from dan.cxx import ldd
        for lib, lib_path in await ldd.get_runtime_dependencies(t):
            if lib_path or not_found:
                print(' ' * 7, lib, '=>', lib_path or 'not found')

@cli.command()
@common_opts
@click.argument('TARGETS', nargs=-1, type=click.TargetParamType())
@pass_context
async def clean(ctx, **kwargs):
    """Clean generated stuff"""
    async with ctx(**kwargs) as make:
        await make.clean()


@cli.command()
@common_opts
@click.argument('TARGETS', nargs=-1, type=click.TargetParamType())
@pass_context
async def run(ctx, **kwargs):
    """Run executable(s)"""
    async with ctx(**kwargs) as make:
        rc = await make.run()
        sys.exit(rc)


@cli.command()
@common_opts
@click.argument('TARGETS', nargs=-1, type=click.TargetParamType())
@pass_context
async def test(ctx, **kwargs):
    """Run tests"""
    async with ctx(**kwargs) as make:
        rc = await make.test()
        sys.exit(rc)


@cli.command()
@click.option('-s', '--script',
              help='Use a source script to resolve compilation environment')
@click.option('-p', '--path', 'paths',
              help='Use given path for compilers lookup', multiple=True, type=click.Path(exists=True, file_okay=False))
@click.option('--verbose', '-v', count=True,
              help='Pring debug informations.', envvar='DAN_VERBOSE')
async def scan_toolchains(script: str, paths: list[str], verbose, **kwargs):
    """Scan system toolchains"""
    set_terminal_mode(TerminalMode.BASIC)
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
            logging.getLogger().warning('unknown verbosity level: %s, using INFO', verbose)
            log_level = logging.INFO
    logging.getLogger().setLevel(log_level)
    from dan.cxx.detect import create_toolchains, load_env_toolchain
    if script:
        load_env_toolchain(script)
    else:
        create_toolchains(paths if len(paths) else None)

@cli.command()
@common_opts
@click.argument('TARGETS', nargs=-1, type=click.TargetParamType())
@pass_context
async def env(ctx: CommandsContext, **kwargs):
    """Show environment."""
    kwargs.update({'quiet': True, 'no_status': True})
    async with ctx(**kwargs) as make:
        for k, v in make.env.items():
            click.echo(f'{k}={v}')


@cli.command()
@common_opts
@click.argument('TARGETS', nargs=-1, type=click.TargetParamType())
@pass_context
async def shell(ctx: CommandsContext, **kwds):
    """Open a new shell with suitable environment."""
    from dan.core.runners import sync_run
    async with ctx(**kwds) as make:
        env = dict(os.environ)
        for k, v in make.env.items():
            env[k] = v

        click.logger.info('entering dan shell...')
        click.logger.debug('env: %s', env)
        
        sync_run('bash', cwd=make.context().root.build_path, env=env, pipe=False)


@cli.group()
def code():
    """VS-Code specific commands"""


# from dan.core.bench import benchmark, report_all

@code.command()
@common_opts
@click.argument('CONTEXT', nargs=-1)
@pass_context
async def get_targets(ctx: CommandsContext, **kwargs):
    kwargs.update({'quiet': True, 'diags': True, 'no_status': True})
    # with benchmark('get-targets') as bench:
        # bench.begin('make')
    async with ctx(**kwargs) as make:
            # bench.end()
        out = []
        targets = make.context().root.all_targets
            # with bench('load-dependencies'):
        async with asyncio.TaskGroup() as g:
            for target in targets:
                g.create_task(target.load_dependencies())
            # with bench('gen-output'):
        for target in targets:
            # with bench(f'gen-output-{target.name}'):
                out.append({
                    'name': target.name,
                    'fullname': target.fullname,
                    'buildPath': str(target.build_path),
                    'srcPath': str(target.source_path),
                    'output': str(target.output),
                    'executable': isinstance(target, Executable),
                    'type': type(target).__name__,
                    'env': target.env if isinstance(target, Executable) else None,
                })
            # with bench('json-dump'):
        click.echo(json.dumps(out))
    # report_all()

@code.command()
@common_opts
@click.argument('TARGETS', nargs=-1)
@pass_context
async def get_tests(ctx: CommandsContext, **kwargs):
    kwargs.update({'quiet': True, 'diags': True, 'no_status': True})
    async with ctx(**kwargs) as make:
        out = list()
        for t in make.context().root.all_tests:
            out.append(t.fullname)
            if len(t) > 1:
                for c in t.cases:
                    out.append(f'{t.fullname}:{c.name}')
        click.echo(json.dumps(out))


@code.command()
@common_opts
@click.option('--pretty', is_flag=True)
@click.argument('TARGETS', nargs=-1)
@pass_context
async def get_test_suites(ctx: CommandsContext, pretty, **kwargs):
    kwargs.update({'quiet': True, 'diags': True, 'no_status': True})
    async with ctx(**kwargs) as make:
        code = Code(make)
        click.echo(code.get_test_suites(pretty))


@code.command()
def get_toolchains(**kwargs):
    click.echo(json.dumps(list(Make.toolchains()['toolchains'].keys())))

@code.command()
@common_opts
@pass_context
async def get_buildfiles(ctx: CommandsContext, **kwargs):
    kwargs.update({'quiet': True, 'diags': True, 'no_status': True})
    async with ctx(**kwargs) as make:
        builfiles = [f.__file__ for f in make.makefiles()]        
        click.echo(json.dumps(builfiles))


@code.command()
@click.option('--for-install', is_flag=True, help='Build for install purpose (will update rpaths [posix only])')
@click.option('--context', '-c', 'contexts', type=click.ContextParamType(), multiple=True,
              help='Use this context')
@common_opts
@click.option('--force', '-f', is_flag=True,
              help='Clean before building')
@click.argument('TARGETS', nargs=-1, type=click.TargetParamType())
@pass_context
async def build(ctx: CommandsContext, force=False, **kwargs):
    """Build targets (vscode version)"""
    async with ctx(**kwargs, diags=True, code=True) as make:
        if force:
            await make.clean()
        await make.build()


@code.command()
@minimal_options
@click.argument('SOURCES', nargs=-1, type=click.Path(exists=True, dir_okay=False, resolve_path=True))
@pass_context
async def get_source_configuration(ctx: CommandsContext, sources, **kwargs):
    kwargs.update({'quiet': True, 'diags': True, 'no_status': True})
    async with ctx(**kwargs) as make:
        code = Code(make)
        click.echo(await code.get_sources_configuration(sources))


@code.command()
@minimal_options
@pass_context
async def get_workspace_browse_configuration(ctx: CommandsContext, **kwargs):
    kwargs.update({'quiet': True, 'diags': True, 'no_status': True})
    async with ctx(**kwargs) as make:
        code = Code(make)
        click.echo(await code.get_workspace_browse_configuration())


@code.command()
@common_opts
@click.argument('CONTEXT')
@pass_context
async def get_options(ctx: CommandsContext, context, **kwargs):
    """List options"""
    kwargs.update({'quiet': True, 'diags': True, 'no_status': True, 'contexts': [context]})
    async with ctx(**kwargs) as make:
        opts = list()
        for o in make.all_options():
            opts.append({
                'name': o.name,
                'fullname': o.fullname,
                'help': o.help,
                'type': o.type.__name__,
                'value': o.value,
                'default': o.default
            })
        click.echo(json.dumps(opts))

@cli.result_callback()
@pass_context
async def process_result(ctx, result, **kwargs):
    await Cache.save_all()

def main():
    import sys
    try:
        loop = asyncio.new_event_loop()
        cli(auto_envvar_prefix='DAN')
    except Exception as err:
        click.logger.error(str(err))
        _ex_type, _ex, tb = sys.exc_info()
        import traceback
        click.logger.debug(' '.join(traceback.format_tb(tb)))
        try:
            # wait asyncio loop to terminate
            loop.run_until_complete()
        except Exception:
            pass
        asyncio.run(Cache.save_all())
        return -1
    finally:
        term = term_manager()
        term.stop()
        if term._thread:
            term._thread.get_loop().run_until_complete(term._thread)
