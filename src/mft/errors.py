#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""errors.py — mft 库的跨层异常词汇。

只放"多个层都要引用"的异常类型，避免下层为上层定义异常、或上层反向 import 下层。
当前成员：BuildCancelled —— 索引构建的取消契约。
"""


class BuildCancelled(Exception):
    """Raised from an on_batch progress hook to abort a walk (a newer scope request won).

    Deliberately an exception: the hook is the only point where the traversal re-enters
    upper-layer code, so unwinding from there skips both the cache write and the publish.

    Lives here (not in the walker) because it is the shared vocabulary between the
    producer (index.walk_source raises it), the cache layer (documents that a cancelled
    build writes nothing) and every consumer that installs the hook (web_ui catches it).
    """
