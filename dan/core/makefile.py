from functools import cached_property
import functools
from pathlib import Path
import sys

from dan.core.cache import Cache
from dan.core.target import Options, Target
from dan.core.test import Test


class MakeFile(sys.__class__):

    def _setup(self,
               name: str,
               source_path: Path,
               build_path: Path,
               requirements: 'MakeFile' = None,
               parent: 'MakeFile' = None) -> None:
        self.name = name
        self.description = None
        self.version = None
        self.source_path = source_path
        self.build_path = build_path
        self.__requirements = requirements
        self.parent = parent
        self.__cache: Cache = None
        self.children: list[MakeFile] = list()
        if self.name != 'dan-requires' and self.parent:
            self.parent.children.append(self)
        self.options = Options(self)
        self.__targets: set[Target] = set()
        self.__tests: set[Test] = set()

        from dan.core.include import context
        self.context = context

    @property
    def fullname(self):
        return f'{self.parent.fullname}.{self.name}' if self.parent else self.name

    @property
    def cache(self) -> Cache:
        if not self.__cache:
            self.__cache = Cache(
                self.build_path / f'{self.name}.cache', cache_name=self.fullname, binary=True)
        return self.__cache

    __target_fullnames = list()
    __test_fullnames = list()
    def register(self, cls: type[Target | Test]):
        """Register Target/Test class"""
        t = cls()
        if issubclass(cls, Target):
            # if t.fullname in MakeFile.__target_fullnames:
            #     raise RuntimeError(f'duplicate target name: {t.fullname}')
            MakeFile.__target_fullnames.append(t.fullname)
            self.__targets.add(t)
        if issubclass(cls, Test):
            # if t.fullname in MakeFile.__test_fullnames:
            #     raise RuntimeError(f'duplicate test name: {t.fullname}')
            MakeFile.__test_fullnames.append(t.fullname)
            self.__tests.add(t)
        return cls

    def wraps(self, cls: type[Target]):
        def decorator(new_cls: type[Target]):
            assert issubclass(
                new_cls, cls), 'Target wrapper must inherit from original target'
            for t in self.__targets:
                if type(t) == cls:
                    self.__targets.remove(t)
                    return new_cls
            assert False, 'Original target has not been registered'
        return decorator


    @functools.cache
    def __find(self, name_or_class) -> Target:
        if isinstance(name_or_class, type):
            def check(t: Target):
                return type(t) == name_or_class
        else:
            def check(t: Target):
                return t.name == name_or_class
        for t in self.targets:
            if check(t):
                return t
        for c in self.children:
            t = c.__find(name_or_class)
            if t:
                return t

    def find(self, name_or_class) -> Target:
        """Find a target.

        Args:
            name (str): The target name to find.

        Returns:
            Target: The found target or None.
        """
        t = self.__find(name_or_class)
        if t is not None:
            return t
        
        if self.parent:
            return self.parent.find(name_or_class)

    def __getitem__(self, name_or_class) -> Target:
        return self.find(name_or_class)

    @property
    def requirements(self):
        if self.name == 'dan-requires':
            return self
        if self.__requirements is not None:
            return self.__requirements
        elif self.parent is not None:
            return self.parent.requirements

    @property
    def pkgs_path(self):
        if self.requirements:
            return self.requirements.parent.build_path / 'pkgs'
        else:
            return self.build_path / 'pkgs'

    @requirements.setter
    def requirements(self, value: 'MakeFile'):
        self.__requirements = value

    @property
    def targets(self) -> set[Target]:
        return self.__targets

    @property
    def all_targets(self) -> set[Target]:
        targets = self.targets
        for c in self.children:
            targets.update(c.all_targets)
        return targets

    @property
    def tests(self) -> set[Test]:
        return self.__tests

    @property
    def all_tests(self) -> set[Test]:
        tests = self.tests
        for c in self.children:
            tests.update(c.all_tests)
        return tests

    @property
    def executables(self):
        from dan.cxx import Executable
        return [target for target in self.targets if issubclass(target, Executable)]

    @property
    def all_executables(self):
        executables = self.executables
        for c in self.children:
            executables.update(c.all_executables)
        return executables

    @property
    def installed(self):
        return [target for target in self.targets if target.installed == True]

    @property
    def all_installed(self):
        return [target for target in self.all_targets if target.installed == True]

    @property
    def default(self):
        return [target for target in self.targets if target.default == True]

    @property
    def all_default(self):
        return [target for target in self.all_targets if target.default == True]
    
    @cached_property
    def root(self):
        m = self
        while m.parent is not None:
            m = m.parent
        return m
