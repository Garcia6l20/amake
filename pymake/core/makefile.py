from functools import cached_property
from pathlib import Path
import sys

from pymake.core.cache import Cache
from pymake.core.target import Options, Target
from pymake.core.test import Test


class MakeFile(sys.__class__):

    def _setup(self,
               name: str,
               source_path: Path,
               build_path: Path,
               requirements: 'MakeFile' = None) -> None:
        self.name = name
        self.description = None
        self.version = None
        self.source_path = source_path
        self.build_path = build_path
        self.__requirements = requirements
        self.parent: MakeFile = self.parent if hasattr(
            self, 'parent') else None
        self.__cache: Cache = None
        self.children: list[MakeFile] = list()
        if self.name != 'requirements' and self.parent:
            self.parent.children.append(self)
        self.options = Options(self)
        self.__targets: set[Target] = set()
        self.__tests: set[Test] = set()

    @cached_property
    def fullname(self):
        return f'{self.parent.fullname}.{self.name}' if self.parent else self.name

    @property
    def cache(self) -> Cache:
        if not self.__cache:
            self.__cache = Cache(self.build_path / f'{self.name}.cache.yaml')
        return self.__cache

    def register(self, cls: type[Target | Test]):
        if issubclass(cls, Target):
            self.__targets.add(cls)
        if issubclass(cls, Test):
            self.__tests.add(cls)
        return cls
    
    def find(self, name) -> Target:
        """Find a target.

        Args:
            name (str): The target name to find.

        Returns:
            Target: The found target or None.
        """
        for t in self.targets:
            if t.name == name:
                return t
        for c in self.children:
            t = c.find(name)
            if t:
                return t

    @property
    def requirements(self):
        if self.__requirements is not None:
            return self.__requirements
        elif self.parent is not None:
            return self.parent.requirements

    @requirements.setter
    def requirements(self, value: 'MakeFile'):
        self.__requirements = value

    @property
    def targets(self):
        return self.__targets

    @property
    def all_targets(self) -> list[type[Target]]:
        targets = self.targets
        for c in self.children:
            targets.update(c.all_targets)
        return targets

    @property
    def tests(self):
        return self.__tests

    @property
    def all_tests(self):
        tests = self.tests
        for c in self.children:
            tests.update(c.all_tests)
        return tests

    @property
    def executables(self):
        from pymake.cxx import Executable
        return {target for target in self.targets if issubclass(target, Executable)}

    @property
    def all_executables(self):
        executables = self.executables
        for c in self.children:
            executables.update(c.all_executables)
        return executables

    @property
    def installed(self):
        return {target for target in self.targets if target.installed == True}

    @property
    def all_installed(self):
        return {target for target in self.all_targets if target.installed == True}

    @property
    def default(self):
        return {target for target in self.targets if target.default == True}

    @property
    def all_default(self):
        return {target for target in self.all_targets if target.default == True}
