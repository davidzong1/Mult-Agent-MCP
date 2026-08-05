"""
Multi-Agent MCP — TUI worker 装饰器（默认不因异常终止 App）
=========================================================

Textual 的 ``@work`` 默认 ``exit_on_error=True``：worker 里任何未捕获异常都会
走 ``app._handle_exception()``，**直接终止整个 App**（带 traceback 退出）。

这个默认值对本项目是灾难性的：几乎所有交互动作（编辑成员、保存代理配置、
启停终端…）都是 ``@work`` worker，任何一次保存里的 TypeError 都会让用户丢掉
整个 TUI 会话。典型案例见 tests/test_tui_select_blank_p0.py —— 成员编辑里选中
Select 空选项，保存时 ``json.dump`` 抛 TypeError，整个 TUI 崩溃。

本模块导出一个同名 ``work``，只改一个默认值：``exit_on_error=False``。异常改由
``TeamManagerApp.on_worker_state_changed`` 捕获并通知用户 —— "这一次操作失败"
而不是"整个程序崩溃"。

这是安全网，不是免检牌：各动作仍应自己校验输入。
"""
from __future__ import annotations

from textual import work as _textual_work

__all__ = ["work"]


def work(*args, **kwargs):
    """``textual.work``，但默认 ``exit_on_error=False``。

    同时支持裸用（``@work``）和带参用（``@work(thread=True)``）。
    显式传入的 ``exit_on_error`` 优先——需要"失败即退出"语义时仍可覆盖。
    """
    # 裸用：@work —— 唯一位置参数就是被装饰的函数
    if len(args) == 1 and not kwargs and callable(args[0]):
        return _textual_work(exit_on_error=False)(args[0])
    kwargs.setdefault("exit_on_error", False)
    return _textual_work(*args, **kwargs)
