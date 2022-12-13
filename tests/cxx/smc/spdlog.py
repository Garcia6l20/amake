import os
from pymake import self, include
from pymake.cxx import Library
from pymake.smc import GitSources

fmt, = include('fmt')

gitspdlog = GitSources(
    'gitspdlog', 'https://github.com/gabime/spdlog.git', 'v1.11.0')

spdlog_src = gitspdlog.output / 'src'
spdlog_inc = gitspdlog.output / 'include'

spdlog = Library('spdlog',
                 sources=[
                     spdlog_src / 'async.cpp',
                     spdlog_src / 'cfg.cpp',
                     spdlog_src / 'color_sinks.cpp',
                     spdlog_src / 'file_sinks.cpp',
                     spdlog_src / 'stdout_sinks.cpp',
                     spdlog_src / 'spdlog.cpp',
                 ],
                 includes=[spdlog_inc],
                 compile_definitions=['SPDLOG_COMPILED_LIB'],
                 preload_dependencies=[gitspdlog],
                 dependencies=[fmt],
                 all=False)

if os.name == 'posix':
    spdlog.link_libraries.add('pthread', public=True)

self.export(spdlog, fmt)
