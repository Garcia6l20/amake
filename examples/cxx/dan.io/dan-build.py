import hashlib
from dan import requires
from dan.cxx import Executable
from dan.testing import Test, Case

catch2, = requires('catch2 = 3')

cpp_std = 17

class TestFmt(Executable):
    name = 'test-fmt'
    sources = 'test_fmt.cpp'
    dependencies = [
        'fmt = 9',
    ]

@catch2.discover_tests
class UseCatch2(Executable):
    name = 'catch2-example'
    sources = 'test_catch2.cpp',
    dependencies = [
        'catch2:catch2-with-main',
        'spdlog >= 1.11',
    ]


class TestSpdlog(Test, Executable):
    name = 'spdlog-example'
    sources = 'test_spdlog.cpp',
    private_includes= '.',
    dependencies = (
        'spdlog = 1',
        'fmt = 9',
    )


class TestMbedTLS(Test, Executable):
    name = 'mbedtls-example'
    sources = 'test_mbedtls.cpp',
    dependencies = (
        'mbedtls:mbedcrypto = 3',
        'fmt = 9',
    )

    def make_expected_result(case: Case):
        sha = hashlib.sha256()
        for arg in case.args:
            sha.update(arg.encode())
        return f'SHA-256: {sha.hexdigest()}'

    cases = [
        Case('hello', 'hello', expected_output=make_expected_result),
        Case('hello-dan', 'hello', 'dan', expected_output=make_expected_result),
    ]

class TestBoost(Test, Executable):
    name = 'boost-example'
    sources= 'test_boost.cpp',
    dependencies= 'boost:boost-headers >= 1.82',
    cases = [
        Case('42-12', 42, 12, expected_result=6),
        Case('44-8', 44, 8, expected_result=4),
        Case('142-42', 142, 42, expected_result=2),
    ]
