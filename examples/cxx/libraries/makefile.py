from pymake import self, generator
from pymake.cxx import Library, Executable, LibraryType, target_toolchain

target_toolchain.cpp_std = 17

sources = ['lib.cpp']
includes = [self.source_path, self.build_path]

# makefile-scope option
self.options.add('library_type', LibraryType.AUTO)


@generator('lib-config.hpp')
async def config(self):
    from pymake.core import aiofiles
    is_linux = target_toolchain.has_definition('__linux')
    has_this_flag_does_not_exist = target_toolchain.has_cxx_compile_options(
        '-this-flag-does-not-exist')
    has_time_h = target_toolchain.has_include('<linux/time.h>')
    has_kernel_timespec = target_toolchain.can_compile('''
        #include <linux/time.h>
        #include <linux/time_types.h>
        #include <cstdint>
        struct __kernel_timespec ts;
        static_assert(sizeof(ts) == 2 * sizeof(uint64_t));
        ''')
    async with aiofiles.open(self.output, 'w') as f:
        await f.write(f'''#pragma once

#define HAS_THIS_FLAG_DOES_NOT_EXIST {'true' if has_this_flag_does_not_exist else 'false'}
#define HAS_TIME_H {'true' if has_time_h else 'false'}
#define HAS_KERNEL_TIMESPEC {'true' if has_kernel_timespec else 'false'}
#define IS_LINUX {'true' if is_linux else 'false'}

''')


opts = []

if target_toolchain.type in ['gcc', 'clang']:
    may_have_opts = [
        '-Wstringop-overflow',
        '-Warray-bounds',
    ]
    for opt in may_have_opts:
        if target_toolchain.has_cxx_compile_options(opt):
            opts.append(opt)

lib = Library('simplelib', sources=sources, includes=includes,
              library_type=self.options.library_type, dependencies=[config])
lib.compile_options.add(*opts, public=True)

exe = Executable('pymake-simple-lib', sources=['main.cpp'], dependencies=[lib])

self.install(exe, lib)
