from dan import Target, asyncio
from dan.core.pathlib import Path

from dan.cxx.unix_toolchain import UnixToolchain
from dan.cxx.detect import Compiler, create_toolchain
from dan.cxx.base_toolchain import ToolchainSettings

import os


class Toolchain(Target, internal=True):

    host = False
    base_class = UnixToolchain
    arch = None
    cc_path = None
    settings = ToolchainSettings()
    
    @property
    def cached_toolchain(self):
        return self.cache.get('toolchain-data')

    @property
    def context_toolchain_name(self):
        return f'cxx_{"host" if self.host else "target"}_toolchain'
    
    @property
    def env(self):
        env = dict(os.environ)
        return {key: env[key] for key in ('PATH', 'LC_LOCAL', 'LD_LIBRARY_PATH') if key in env}
    
    @asyncio.cached
    async def initialize(self):            
        await super().initialize()

        if self.up_to_date:
            toolchain = self.base_class(self.cached_toolchain, tools=dict(), settings=self.settings)
            self.context.set(self.context_toolchain_name, toolchain)

    def __build__(self):
        cc = Compiler(Path(self.cc_path), env=self.env)
        tname, tdata = create_toolchain(cc, self)
        self.cache['toolchain-name'] = tname
        self.cache['toolchain-data'] = tdata
        toolchain = self.base_class(tdata, tools=dict(), settings=self.settings)
        self.context.set(self.context_toolchain_name, toolchain)


