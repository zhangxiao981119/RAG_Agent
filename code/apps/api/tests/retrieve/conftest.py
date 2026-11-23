"""`tests/retrieve` 的兼容层：`fake_settings()` 已上移到 `tests/fake_settings.py`。

子目录 conftest 里的名字只对该目录及子目录的测试可见 —— `tests/wiki/`、`tests/scenario/`
等读不到本文件，所以 helper 必须做成普通模块才能全仓复用。这里只做 re-export，让本目录
既有的 `from tests.retrieve.conftest import fake_settings` 继续可用（历史 import 不用改）。

★ 新写的用例请直接 `from tests.fake_settings import fake_settings`。
"""
from tests.fake_settings import fake_settings  # noqa: F401
