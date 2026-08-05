"""
P0 回归：Select 空选项("Select" 占位行)导致 TUI 崩溃。
=====================================================

复现路径
--------
团队成员管理页 → 按 e 进入成员编辑 → "代理模式"下拉选中空的 "Select" 行 →
点保存 → 整个 TUI 带 traceback 退出。

根因链
------
1. Textual ``Select`` 默认 ``allow_blank=True``，下拉里多出一行占位项；
   用户选中它后 ``select.value`` 是 ``Select.NULL``（NoSelection 哨兵）。
2. 该哨兵 **truthy**、**不是 str**、**不可 JSON 序列化**。truthy 让
   ``value or "inherit"`` 之类的兜底全部失效，于是它原样进入 dismiss 载荷。
3. ``action_edit_member`` 用 ``result.get("proxy_mode", "inherit")`` 取值——键
   存在，默认值用不上——把哨兵写进 member dict。
4. ``save_data`` → ``atomic_json_write`` → ``json.dump`` 抛 TypeError。调用方是
   Textual ``@work`` worker，异常未捕获 → 整个 App 崩溃。

因此凡是"带空选项且裸读 .value"的 Select 都会崩，不止代理模式一处。

修复
----
- ``_normalize_select_value`` / ``_select_value``：所有 Select 读取的唯一入口
- ``_ensure_option``：allow_blank=False 前保证原值命中选项（自定义 agent /
  已删除 profile 不被静默改写，也不触发 InvalidSelectValueError）
- 枚举型 Select 一律 ``allow_blank=False``：空选项从 UI 上就不存在
- ``SelectSafeDismissMixin``：dismiss 载荷兜底清洗，防止将来新增 Select 复发
"""

import json
import unittest
from unittest import mock

from textual.widgets import Select

from tui.tui_dialogs import (
    AGENT_CHOICES,
    AddMemberDialog,
    AgentUserEditDialog,
    CreateTeamDialog,
    EditMemberDialog,
    SelectSafeDismissMixin,
    TeamProxyDialog,
    _claude_mcp_configured,
    _ensure_option,
    _normalize_select_value,
    _scrub_no_selection,
    _select_value,
    _selected_profile_key,
)


class _FakeSelect:
    """只提供 .value 的最小替身（与 test_agent_user.py 中的用法一致）。"""

    def __init__(self, value):
        self.value = value


# ============================================================
# 根因确认
# ============================================================

class RootCauseTests(unittest.TestCase):
    """把崩溃链上的每一环钉死，防止有人"优化"掉归一化。"""

    def test_blank_sentinel_is_truthy_so_or_fallbacks_fail(self):
        """哨兵 truthy —— 这是 `value or default` 兜底失效的原因。"""
        self.assertTrue(bool(Select.NULL),
                        "NoSelection 若变 falsy 本测试可放宽，但归一化仍须保留")

    def test_blank_sentinel_is_not_a_string(self):
        self.assertNotIsInstance(Select.NULL, str)

    def test_blank_sentinel_breaks_json_dump(self):
        """哨兵进 teams_data.json → save_data 抛 TypeError → TUI 崩溃。"""
        with self.assertRaises(TypeError):
            json.dumps({"proxy_mode": Select.NULL})

    def test_dict_get_default_does_not_save_you(self):
        """键存在时 .get(k, default) 拿不到 default —— 旧代码的第二个坑。"""
        payload = {"proxy_mode": Select.NULL}
        self.assertIs(payload.get("proxy_mode", "inherit"), Select.NULL)


# ============================================================
# 归一化 helper
# ============================================================

class NormalizeSelectValueTests(unittest.TestCase):

    def test_blank_returns_default(self):
        self.assertEqual(_normalize_select_value(Select.NULL), "")
        self.assertEqual(_normalize_select_value(Select.NULL, "inherit"), "inherit")

    def test_none_returns_default(self):
        self.assertEqual(_normalize_select_value(None, "claude"), "claude")

    def test_real_values_pass_through(self):
        self.assertEqual(_normalize_select_value("enabled", "inherit"), "enabled")
        self.assertEqual(_normalize_select_value("__none__"), "__none__")

    def test_empty_string_is_a_legit_value_not_the_default(self):
        """'' 是"系统默认"这个真实选项，不能被 default 顶掉。"""
        self.assertEqual(_normalize_select_value("", "fallback"), "")

    def test_result_is_always_str(self):
        for raw in (Select.NULL, None, "", "claude"):
            self.assertIsInstance(_normalize_select_value(raw), str)

    def test_select_value_reads_widget(self):
        self.assertEqual(_select_value(_FakeSelect(Select.NULL), "inherit"), "inherit")
        self.assertEqual(_select_value(_FakeSelect("disabled"), "inherit"), "disabled")

    def test_selected_profile_key_semantics_preserved(self):
        """已有 helper 改为委托后语义必须不变。"""
        self.assertEqual(_selected_profile_key(_FakeSelect(Select.NULL)), "")
        self.assertEqual(_selected_profile_key(_FakeSelect("")), "")
        self.assertEqual(_selected_profile_key(_FakeSelect("alice")), "alice")
        self.assertEqual(_selected_profile_key(_FakeSelect("__none__")), "__none__")


class EnsureOptionTests(unittest.TestCase):

    def test_missing_value_is_appended(self):
        opts = _ensure_option([("claude", "claude")], "codex --yolo")
        self.assertIn("codex --yolo", [v for _, v in opts])

    def test_present_value_is_noop(self):
        base = [("claude", "claude")]
        self.assertEqual(_ensure_option(base, "claude"), base)

    def test_empty_value_is_noop(self):
        base = [("系统默认", "")]
        self.assertEqual(_ensure_option(base, ""), base)

    def test_original_list_not_mutated(self):
        base = [("claude", "claude")]
        _ensure_option(base, "custom-cmd")
        self.assertEqual(len(base), 1, "_ensure_option 不应就地改调用方的列表")


class ScrubNoSelectionTests(unittest.TestCase):

    def test_bare_sentinel(self):
        self.assertEqual(_scrub_no_selection(Select.NULL), "")

    def test_nested_containers(self):
        payload = {
            "a": Select.NULL,
            "b": [Select.NULL, "ok"],
            "c": {"d": Select.NULL},
            "e": (Select.NULL, 1),
        }
        cleaned = _scrub_no_selection(payload)
        self.assertEqual(cleaned, {"a": "", "b": ["", "ok"], "c": {"d": ""}, "e": ("", 1)})
        json.dumps(cleaned)  # 不抛即通过

    def test_non_sentinel_values_untouched(self):
        payload = {"n": 1, "s": "x", "b": True, "none": None}
        self.assertEqual(_scrub_no_selection(payload), payload)


class DismissMixinTests(unittest.TestCase):
    """mixin 必须真的拦到 dismiss，且必须排在 ModalScreen 前面。"""

    def test_mixin_scrubs_payload(self):
        seen = {}

        class _Base:
            def dismiss(self, result=None):
                seen["result"] = result
                return result

        class _Dialog(SelectSafeDismissMixin, _Base):
            pass

        _Dialog().dismiss({"proxy_mode": Select.NULL, "agent": "claude"})
        self.assertEqual(seen["result"], {"proxy_mode": "", "agent": "claude"})
        json.dumps(seen["result"])

    def test_mixin_preserves_no_arg_call(self):
        seen = {}

        class _Base:
            def dismiss(self, result="__unset__"):
                seen["result"] = result

        class _Dialog(SelectSafeDismissMixin, _Base):
            pass

        _Dialog().dismiss()
        self.assertEqual(seen["result"], "__unset__",
                         "无参 dismiss 不能被 mixin 篡改成 None")

    def test_form_dialogs_use_the_mixin(self):
        for cls in (CreateTeamDialog, AddMemberDialog, EditMemberDialog,
                    TeamProxyDialog, AgentUserEditDialog):
            with self.subTest(cls=cls.__name__):
                self.assertTrue(issubclass(cls, SelectSafeDismissMixin),
                                f"{cls.__name__} 必须继承 SelectSafeDismissMixin")
                mro = cls.__mro__
                from textual.screen import ModalScreen
                self.assertLess(mro.index(SelectSafeDismissMixin), mro.index(ModalScreen),
                                "mixin 必须排在 ModalScreen 之前才能拦到 dismiss")


# ============================================================
# 真实弹窗：空选项已从 UI 移除
# ============================================================

_TEAM = "p0_team"
_MOCK_PROFILES: dict = {}


def _push(app, screen):
    return app.push_screen(screen)


class BlankOptionRemovedTests(unittest.IsolatedAsyncioTestCase):
    """枚举型 Select 不再允许空选择 —— 用户根本选不到那一行。"""

    async def _open(self, dialog):
        from textual.app import App
        app = App()
        return app, dialog

    async def test_edit_member_selects_disallow_blank(self):
        from textual.app import App

        with mock.patch("tui.tui_dialogs._agent_user_profiles", return_value=_MOCK_PROFILES):
            app = App()
            dialog = EditMemberDialog(
                "alice", current_role="coder", current_agent="claude",
                current_proxy_mode="enabled", current_agent_user="", team_name=_TEAM,
            )
            async with app.run_test(size=(100, 30)) as pilot:
                await _push(pilot.app, dialog)
                await pilot.pause(0.3)
                for sel_id in ("#agent", "#proxy_mode", "#agent_user"):
                    with self.subTest(select=sel_id):
                        sel = pilot.app.screen.query_one(sel_id, Select)
                        self.assertFalse(
                            sel._allow_blank,
                            f"{sel_id} 仍允许空选择 —— 崩溃入口未关闭",
                        )
                        self.assertIsNot(sel.value, Select.NULL)

    async def test_add_member_selects_disallow_blank(self):
        from textual.app import App

        with mock.patch("tui.tui_dialogs._agent_user_profiles", return_value=_MOCK_PROFILES):
            app = App()
            dialog = AddMemberDialog(default_agent="claude", team_name=_TEAM)
            async with app.run_test(size=(100, 30)) as pilot:
                await _push(pilot.app, dialog)
                await pilot.pause(0.3)
                for sel_id in ("#agent", "#proxy_mode", "#agent_user"):
                    with self.subTest(select=sel_id):
                        self.assertFalse(
                            pilot.app.screen.query_one(sel_id, Select)._allow_blank)

    async def test_create_team_and_proxy_selects_disallow_blank(self):
        from textual.app import App

        app = App()
        async with app.run_test(size=(100, 30)) as pilot:
            await _push(pilot.app, CreateTeamDialog())
            await pilot.pause(0.3)
            for sel_id in ("#agent", "#proxy_enabled"):
                with self.subTest(select=sel_id):
                    self.assertFalse(
                        pilot.app.screen.query_one(sel_id, Select)._allow_blank)

        app2 = App()
        async with app2.run_test(size=(100, 30)) as pilot:
            await _push(pilot.app, TeamProxyDialog(_TEAM, {}, current_member="alice"))
            await pilot.pause(0.3)
            self.assertFalse(
                pilot.app.screen.query_one("#proxy_action", Select)._allow_blank)

    async def test_agent_user_provider_still_allows_blank_but_takeover_does_not(self):
        """provider 是三态语义（未选 = 必须报错提示），空选项要保留；
        接管开关是二元枚举，空选项必须去掉。"""
        from textual.app import App

        app = App()
        dialog = AgentUserEditDialog()  # 新建：agent_type 为空
        async with app.run_test(size=(80, 30)) as pilot:
            await _push(pilot.app, dialog)
            await pilot.pause(0.3)
            self.assertTrue(pilot.app.screen.query_one("#agent_type", Select)._allow_blank)
            self.assertFalse(pilot.app.screen.query_one("#takeover", Select)._allow_blank)


class LegacyProfileDialogOpensTests(unittest.IsolatedAsyncioTestCase):
    """旧版 profile(无 agent_type) 编辑：provider 初值是 Select.NULL，
    若 allow_blank 跟着 is_new 走就会是 False → 构造期 InvalidSelectValueError。"""

    async def test_legacy_profile_edit_dialog_opens(self):
        from textual.app import App

        app = App()
        # 有 user_key → _is_new=False；无 agent_type → 旧版 profile 编辑路径
        dialog = AgentUserEditDialog(user_key="legacy")
        self.assertFalse(dialog._is_new)
        self.assertTrue(dialog._provider_editable)
        async with app.run_test(size=(80, 30)) as pilot:
            await _push(pilot.app, dialog)
            await pilot.pause(0.3)
            self.assertIs(pilot.app.screen, dialog,
                          "旧版 profile 编辑弹窗必须能打开（不得抛 InvalidSelectValueError）")
            sel = pilot.app.screen.query_one("#agent_type", Select)
            self.assertTrue(sel._allow_blank)
            self.assertIs(sel.value, Select.NULL)


class CustomAgentValuePreservedTests(unittest.IsolatedAsyncioTestCase):
    """成员的 agent 可能是自定义命令（MCP 侧 member_set_agent 接受任意命令）。
    allow_blank=False 下必须靠 _ensure_option 补进选项，否则弹窗打不开；
    且保存后不能被静默改写成 claude。"""

    async def test_custom_agent_dialog_opens_and_round_trips(self):
        from textual.app import App
        from textual.widgets import Button

        custom = "codex --dangerously-bypass"
        self.assertNotIn(custom, [v for _, v in AGENT_CHOICES])

        with mock.patch("tui.tui_dialogs._agent_user_profiles", return_value=_MOCK_PROFILES):
            app = App()
            dialog = EditMemberDialog(
                "bob", current_role="coder", current_agent=custom,
                current_proxy_mode="inherit", current_agent_user="", team_name=_TEAM,
            )
            async with app.run_test(size=(100, 30)) as pilot:
                await _push(pilot.app, dialog)
                await pilot.pause(0.3)
                self.assertIs(pilot.app.screen, dialog,
                              "自定义 agent 的成员编辑弹窗必须能打开")
                sel = pilot.app.screen.query_one("#agent", Select)
                self.assertEqual(sel.value, custom, "自定义 agent 不能被改写")


class SaveProducesJsonSerializablePayloadTests(unittest.IsolatedAsyncioTestCase):
    """端到端：点保存拿到的载荷必须能过 json.dumps（= save_data 不会崩）。"""

    async def test_edit_member_save_payload_is_serializable(self):
        from textual.app import App

        captured = {}

        with mock.patch("tui.tui_dialogs._agent_user_profiles", return_value=_MOCK_PROFILES):
            app = App()
            dialog = EditMemberDialog(
                "alice", current_role="coder", current_agent="claude",
                current_proxy_mode="enabled", current_agent_user="", team_name=_TEAM,
            )
            orig_dismiss = dialog.dismiss

            def _capture(result=None):
                captured["result"] = result
                return orig_dismiss(result)

            dialog.dismiss = _capture  # type: ignore[method-assign]

            async with app.run_test(size=(100, 30)) as pilot:
                await _push(pilot.app, dialog)
                await pilot.pause(0.3)
                await pilot.click("#btn_save")
                await pilot.pause(0.3)

        result = captured.get("result")
        self.assertIsNotNone(result, "保存应产生 dismiss 载荷")
        json.dumps(result)  # 不抛即通过 —— 这正是原崩溃点
        for key in ("role", "agent", "proxy_mode", "agent_user"):
            with self.subTest(key=key):
                self.assertIsInstance(result[key], str,
                                      f"{key} 必须是 str，不能是 NoSelection")
        self.assertEqual(result["proxy_mode"], "enabled", "原值必须保留")

    async def test_save_falls_back_to_current_value_if_select_goes_blank(self):
        """即使 Select 被外力置空（未来改动 / 第三方），保存也回落到原值而非崩溃。"""
        from textual.app import App

        captured = {}

        with mock.patch("tui.tui_dialogs._agent_user_profiles", return_value=_MOCK_PROFILES):
            app = App()
            dialog = EditMemberDialog(
                "alice", current_role="coder", current_agent="claude",
                current_proxy_mode="enabled", current_agent_user="", team_name=_TEAM,
            )
            orig_dismiss = dialog.dismiss

            def _capture(result=None):
                captured["result"] = result
                return orig_dismiss(result)

            dialog.dismiss = _capture  # type: ignore[method-assign]

            async with app.run_test(size=(100, 30)) as pilot:
                await _push(pilot.app, dialog)
                await pilot.pause(0.3)
                # 绕过 Textual 校验，强行制造"空选择"这一崩溃前提
                pilot.app.screen.query_one("#proxy_mode", Select)._value = Select.NULL
                await pilot.click("#btn_save")
                await pilot.pause(0.3)

        result = captured.get("result")
        self.assertIsNotNone(result)
        json.dumps(result)
        self.assertEqual(result["proxy_mode"], "enabled",
                         "空选择应回落到成员原有 proxy_mode，而不是写入哨兵")


# ============================================================
# 同文件内被重复粘贴覆盖掉的 def（NameError 回归）
# ============================================================

class ClobberedHelperTests(unittest.TestCase):
    """_claude_mcp_configured 的 def 头曾被一份重复的 _highlighted_profile_key
    覆盖，MCP 状态面板一渲染就 NameError。"""

    def test_claude_mcp_configured_is_defined_and_callable(self):
        self.assertTrue(callable(_claude_mcp_configured))

    def test_no_duplicate_top_level_defs_in_module(self):
        import ast
        import pathlib
        import tui.tui_dialogs as mod

        tree = ast.parse(pathlib.Path(mod.__file__).read_text(encoding="utf-8"))
        names = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        dupes = sorted({n for n in names if names.count(n) > 1})
        self.assertEqual(dupes, [], f"模块顶层存在重复 def（会静默覆盖前一个）: {dupes}")


if __name__ == "__main__":
    unittest.main()


# ============================================================
# 最后一道防线：写入 chokepoint
# ============================================================

class AtomicWriteSentinelTests(unittest.TestCase):
    """所有 teams_data.json 写入都汇聚到 atomic_json_write。

    表单层归一化是主修复；这里是安全网 —— 任何漏网的哨兵都落成 ''，
    绝不让一次保存动作把整个 TUI 打崩。
    """

    def setUp(self):
        import tempfile, pathlib
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.target = self.tmp / "teams_data.json"

    def test_sentinel_is_written_as_empty_string(self):
        from common.atomic_write import atomic_json_write

        atomic_json_write(self.target, {
            "teams": {"t": {"members": {"a": {"proxy_mode": Select.NULL}}}}
        })
        got = json.loads(self.target.read_text(encoding="utf-8"))
        self.assertEqual(got["teams"]["t"]["members"]["a"]["proxy_mode"], "",
                         "漏到写入层的哨兵必须落成空串，而不是抛 TypeError")

    def test_permissions_still_enforced(self):
        import stat
        from common.atomic_write import atomic_json_write

        atomic_json_write(self.target, {"k": Select.NULL})
        mode = stat.S_IMODE(self.target.stat().st_mode)
        self.assertEqual(mode, 0o600, "兜底转换不得放松 0600 权限")

    def test_other_unserializable_types_still_raise(self):
        """安全网只认 NoSelection —— 其他不可序列化对象仍须暴露，不掩盖真 bug。"""
        from common.atomic_write import atomic_json_write

        class Weird:
            pass

        with self.assertRaises(TypeError):
            atomic_json_write(self.target, {"k": Weird()})
        self.assertFalse(self.target.exists(),
                         "写入失败不应留下半成品文件")

    def test_normal_data_unaffected(self):
        from common.atomic_write import atomic_json_write

        payload = {"a": 1, "b": "x", "c": [1, 2], "d": {"e": True}, "f": None}
        atomic_json_write(self.target, payload)
        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8")), payload)


class WorkerErrorDoesNotKillAppTests(unittest.IsolatedAsyncioTestCase):
    """@work worker 抛异常 → 通知用户，App 存活。

    没有这个 handler 时，任何交互动作里的未捕获异常都会终止整个 TUI，
    用户丢掉全部会话状态。
    """

    async def test_worker_exception_is_reported_not_fatal(self):
        from tui.tui_worker import work
        import tui.tui_screens as S

        notes = []

        class _App(S.TeamManagerApp):
            def on_mount(self) -> None:
                pass  # 不 push MainScreen，避免触达真实数据文件

            def notify(self, message, *a, **kw):
                notes.append(message)

            @work
            async def boom(self) -> None:
                raise TypeError("Object of type NoSelection is not JSON serializable")

        app = _App()
        async with app.run_test(size=(80, 24)) as pilot:
            pilot.app.boom()
            await pilot.pause(0.5)
            self.assertTrue(pilot.app.is_running,
                            "worker 异常不应终止 App")
            self.assertTrue(any("NoSelection" in n for n in notes),
                            f"应把异常通知用户，实际通知: {notes}")

    async def test_handler_ignores_successful_workers(self):
        from tui.tui_worker import work
        import tui.tui_screens as S

        notes = []

        class _App(S.TeamManagerApp):
            def on_mount(self) -> None:
                pass

            def notify(self, message, *a, **kw):
                notes.append(message)

            @work
            async def fine(self) -> None:
                return None

        app = _App()
        async with app.run_test(size=(80, 24)) as pilot:
            pilot.app.fine()
            await pilot.pause(0.4)
            self.assertEqual(notes, [], "成功的 worker 不应产生错误通知")


class WorkerDecoratorSourceTests(unittest.TestCase):
    """生产模块必须用项目包装版 work，而不是 textual.work。

    ``textual.work`` 默认 ``exit_on_error=True`` —— worker 异常直接终止 App。
    这条断言防止有人"顺手"把 import 改回去，静默恢复崩溃行为。
    """

    def test_production_modules_import_wrapped_work(self):
        import pathlib

        for mod in ("tui/tui_screens.py", "tui/tui_dialogs.py"):
            with self.subTest(module=mod):
                src = pathlib.Path(mod).read_text(encoding="utf-8")
                self.assertIn("from tui.tui_worker import work", src,
                              f"{mod} 必须使用包装版 work")
                for bad in ("from textual import on, work",
                            "from textual import work",
                            "from textual import work, on"):
                    self.assertNotIn(bad, src,
                                     f"{mod} 不得直接导入 textual.work（{bad}）")

    def test_wrapper_defaults_to_non_fatal(self):
        import inspect
        from tui.tui_worker import work

        async def sample(self):
            return None

        # 裸用形式
        decorated = work(sample)
        self.assertFalse(_worker_exit_on_error(decorated),
                         "@work 裸用应默认 exit_on_error=False")

        # 带参形式
        decorated2 = work(thread=False)(sample)
        self.assertFalse(_worker_exit_on_error(decorated2),
                         "@work(...) 带参用应默认 exit_on_error=False")

    def test_explicit_exit_on_error_is_respected(self):
        from tui.tui_worker import work

        async def sample(self):
            return None

        decorated = work(exit_on_error=True)(sample)
        self.assertTrue(_worker_exit_on_error(decorated),
                        "显式传 exit_on_error=True 必须被尊重")


def _worker_exit_on_error(decorated) -> bool:
    """从被 @work 装饰的函数上取出 exit_on_error（跨 textual 版本尽力而为）。

    textual 把参数闭包在 decorator 里，没有公开属性；这里读闭包变量。
    取不到就直接 fail，避免测试静默通过。
    """
    import inspect

    closure = inspect.getclosurevars(decorated)
    for scope in (closure.nonlocals, closure.globals):
        if "exit_on_error" in scope:
            return bool(scope["exit_on_error"])
    raise AssertionError(
        "无法从装饰后的函数取出 exit_on_error —— textual 内部结构可能已变，"
        "请改用行为测试（见 WorkerErrorDoesNotKillAppTests）"
    )
