"""
子页面（ModalScreen / 子页）纵向滚动与低分辨率可达性 —— 隔离 UI 回归测试。
=======================================================================

背景（用户需求）
------------------
修复所有子页面（创建团队、添加成员等）在 1080P 低分辨率下底部交互项被遮挡的问题。
建议建立**全局子页面纵向滚动能力**：任何子页面内容超出 y 轴时，底部主要操作按钮
必须能通过纵向滚动进入可见区，且可聚焦/可点击；滚轮 / PageDown / Tab 的滚动
不得被外层（ModalScreen / App）截断。

本测试钉住的行为契约
-----------------------
1. **1920×1080（高视口）**：底部主要操作按钮完整位于视口内、可见、可聚焦、可点击。
   （Textual size 以字符单元计，1920x1080 @ 8x16 字符 ≈ 240x67 单元。）
2. **更低高度视口（24 / 18 行，模拟 1080P 终端扣除窗口装饰/面板后的有效高度）**：
   - 按钮已在视口内 → 直接通过；
   - 否则必须存在可纵向滚动的祖先容器（overflow-y: auto/scroll 且内容溢出），
     滚动到底后按钮完整进入视口。
   - 滚动后按钮可聚焦（screen.focused == 按钮）、可点击（点击触发其注册处理器，
     可观测副作用 = 弹窗关闭 / 弹出 MessageBox / 进入下一子页 / 结果文本更新）。
3. **PageDown / Tab 不被外层截断**：PageDown 应滚动子页面内部滚动容器；
   Tab 能聚焦到底部主按钮且聚焦后按钮被滚入视口（不被外层挡住）。

覆盖范围（盘点出的 ModalScreen / 子页，共 8 个表单型子页面）
-------------------------------------------------------------
- CreateTeamDialog（创建团队）        —— 当前损坏：`.dialog-form` overflow-y:hidden → 按钮被裁剪
- AddMemberDialog（添加成员）          —— 当前损坏
- EditMemberDialog（编辑成员）          —— 当前损坏
- TeamProxyDialog（代理配置）           —— 当前损坏
- AgentUserEditDialog（编辑 Agent 用户）—— 已修复（固定底部按钮行），本套件绿灯基线
- AgentUserManageDialog（Agent 用户管理）—— 已修复（.dialog-form overflow-y:auto），绿灯基线
- AgentUserRenameDialog（重命名 profile）—— 内容矮，天然可见
- TeamDefaultAgentUserDialog（团队默认）  —— 内容矮，天然可见

隔离性
--------
- `run_test(size=...)` headless，不依赖真实 display。
- 数据经 `data_layer.set_data_file` 重定向到 temp teams_data，绝不触碰真实
  `~/.mult_agent_mcp/teams_data.json`。
- 这些子页面不发起 tmux / 外部进程调用，天然无真实 tmux 依赖。
- 未纳入：McpStatusDialog / AgentMcpConfigDialog（compose 期调用 mcp_server_status /
  codex/claude MCP 配置，触碰真实配置与进程，隔离成本高；且非表单滚动修复对象）。
"""

import tempfile
import unittest
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

from textual.app import App
from textual.widgets import Button, Input, Select

from common import data_layer
from common.data_layer import save_data
from tui.tui_dialogs import (
    AgentUserEditDialog,
    AgentUserManageDialog,
    AgentUserRenameDialog,
    AddMemberDialog,
    CreateTeamDialog,
    EditMemberDialog,
    TeamDefaultAgentUserDialog,
    TeamProxyDialog,
)
from tui.tui_screens import TeamManagerApp

# ---- 视口 ----
# 1920x1080 px @ 8x16 字符单元 ≈ 240 列 x 67 行（高视口：全部内容无需滚动）。
VP_1920X1080 = (240, 67)
# 更低高度视口：模拟 1080P 终端扣除窗口装饰 / 任务栏 / 面板后的有效行数。
VP_LOW = [(100, 24), (100, 18)]

# 测试数据（temp，仅结构所需）
_SEED = {
    "teams": {"t1": {"members": {}}},
    "agent_users": {"claude_p": {"agent_type": "claude"}},
}

# 子页面盘点登记表
# (显示名, 弹窗工厂, 底部主要操作按钮 id, 用于 PageDown 的输入 id 或 None, 点击可观测方式)
# click_observable:
#   "screen"        —— 点击后 app.screen 不再是该弹窗（dismiss / MessageBox / 进入下一子页）
#   "result_label"  —— 点击后 #team_default_result 文本从空变为非空（TeamDefaultAgentUserDialog）
SUBPAGES = [
    ("CreateTeamDialog", lambda: CreateTeamDialog(), "btn_create", "name", "screen"),
    ("AddMemberDialog", lambda: AddMemberDialog(team_name="t1"), "btn_add", "name", "screen"),
    ("EditMemberDialog", lambda: EditMemberDialog("alice", "coder", "claude"), "btn_save", "role", "screen"),
    ("TeamProxyDialog", lambda: TeamProxyDialog("t1", {"host": "127.0.0.1", "port": 7890}), "btn_save", "proxy_action", "screen"),
    ("AgentUserEditDialog", lambda: AgentUserEditDialog(), "btn_save", None, "screen"),
    ("AgentUserManageDialog", lambda: AgentUserManageDialog(), "btn_new", None, "screen"),
    ("AgentUserRenameDialog", lambda: AgentUserRenameDialog("claude_p", {"taken"}), "btn_save", "new_key", "screen"),
    ("TeamDefaultAgentUserDialog", lambda: TeamDefaultAgentUserDialog("t1"), "btn_set_default", "team_default_select", "result_label"),
]


def _make_test_app() -> App[None]:
    """最小 App：复用生产真实 CSS（TeamManagerApp.CSS），不复制粘贴 CSS 常量。"""

    class _TestApp(App[None]):
        CSS = TeamManagerApp.CSS

    return _TestApp()


def _in_viewport(region, viewport_height: int) -> bool:
    """按钮矩形是否完整落在视口高度内。"""
    return region.y >= 0 and region.bottom <= viewport_height


def _scrollable_ancestors(button) -> list:
    """收集主按钮以上、内容溢出量 > 0 的祖先（含 overflow-y 值）。

    用于定位"子页面纵向滚动容器"：机制无关（VerticalScroll / overflow-y:auto 容器均可）。
    """
    out = []
    w = button.parent
    while w is not None:
        max_y = getattr(w, "max_scroll_y", 0) or 0
        if max_y > 0:
            overflow_y = str(getattr(w.styles, "overflow_y", "hidden"))
            out.append((w, max_y, overflow_y))
        w = w.parent
    return out


class SubpageScrollLayoutRegressionTests(IsolatedAsyncioTestCase):
    """全局子页面纵向滚动能力 —— 隔离 UI 回归（真实 CSS + temp 数据，headless）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data_file = self.root / "teams_data.json"
        self.old_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        data_layer.set_data_file(self.data_file)
        save_data(_SEED)

    def tearDown(self):
        data_layer._DATA_FILE_OVERRIDE = self.old_override
        self.tmp.cleanup()

    # ---------- 1. 1920x1080 高视口 ----------

    async def test_1920x1080_primary_button_visible_focusable_clickable(self):
        """高视口（240x67 ≈ 1920x1080）：每个子页面底部主按钮完整可见、可聚焦、可点击。"""
        width, height = VP_1920X1080
        for name, factory, btn_id, _focus_id, observable in SUBPAGES:
            with self.subTest(dialog=name, viewport=VP_1920X1080):
                app = _make_test_app()
                async with app.run_test(size=VP_1920X1080) as pilot:
                    dialog = factory()
                    await pilot.app.push_screen(dialog)
                    await pilot.pause(0.25)
                    btn = pilot.app.screen.query_one(f"#{btn_id}", Button)
                    r = btn.region
                    self.assertTrue(btn.visible, "主按钮应可见")
                    self.assertGreaterEqual(r.y, 0, f"主按钮越出上边界: {r}")
                    self.assertLessEqual(
                        r.bottom, height,
                        f"主按钮被挤出视口底部: region={r} viewport_height={height}",
                    )
                    await self._assert_focus_and_click(pilot, btn, dialog, observable)

    # ---------- 2. 低高度：纵向滚动可达 ----------

    async def test_low_height_primary_button_reachable_by_scroll(self):
        """低高度视口（24/18 行）：主按钮可经纵向滚动进入可见区（否则子页面缺滚动能力）。"""
        for name, factory, btn_id, _focus_id, _observable in SUBPAGES:
            for (width, height) in VP_LOW:
                with self.subTest(dialog=name, viewport=(width, height)):
                    app = _make_test_app()
                    async with app.run_test(size=(width, height)) as pilot:
                        dialog = factory()
                        await pilot.app.push_screen(dialog)
                        await pilot.pause(0.25)
                        btn = pilot.app.screen.query_one(f"#{btn_id}", Button)
                        if _in_viewport(btn.region, height):
                            continue  # 已可见 → 无需滚动即可达
                        ancestors = _scrollable_ancestors(btn)
                        self.assertTrue(
                            ancestors,
                            "主按钮越出视口且不存在可纵向滚动的祖先容器 —— "
                            "子页面缺少纵向滚动能力（待落地全局滚动）",
                        )
                        self.assertTrue(
                            any(overflow_y in ("auto", "scroll") for _, _, overflow_y in ancestors),
                            "存在内容溢出量但 overflow-y 均为 hidden —— "
                            "内容被裁剪而非可滚动（应改为 auto/scroll 或包 VerticalScroll）",
                        )
                        moved = await self._reveal(pilot, btn)
                        self.assertTrue(moved, "滚动后 scroll_y 未变化 —— 滚动能力未生效")
                        self.assertTrue(
                            _in_viewport(btn.region, height),
                            f"滚动到底后主按钮仍未完整进入视口: region={btn.region} "
                            f"viewport_height={height}",
                        )

    # ---------- 3. 低高度：滚动后聚焦 + 点击 ----------

    async def test_low_height_primary_button_focusable_and_clickable_after_reveal(self):
        """低高度视口：滚动进入可见区后，主按钮可聚焦、可点击（处理器触发）。"""
        for name, factory, btn_id, _focus_id, observable in SUBPAGES:
            for (width, height) in VP_LOW:
                with self.subTest(dialog=name, viewport=(width, height)):
                    app = _make_test_app()
                    async with app.run_test(size=(width, height)) as pilot:
                        dialog = factory()
                        await pilot.app.push_screen(dialog)
                        await pilot.pause(0.25)
                        btn = pilot.app.screen.query_one(f"#{btn_id}", Button)
                        if not _in_viewport(btn.region, height):
                            await self._reveal(pilot, btn)
                            await pilot.pause(0.15)
                        self.assertTrue(
                            _in_viewport(btn.region, height),
                            f"前置条件：主按钮应已进入视口（reachable 测试已覆盖滚动）: "
                            f"region={btn.region} viewport_height={height}",
                        )
                        await self._assert_focus_and_click(pilot, btn, dialog, observable)

    # ---------- 4. PageDown 不被外层截断 ----------

    async def test_page_down_not_truncated_by_outer_layer(self):
        """PageDown 应滚动子页面内部滚动容器（而非被外层 ModalScreen/App 截断）。"""
        for name, factory, btn_id, focus_id, _observable in SUBPAGES:
            for (width, height) in VP_LOW:
                with self.subTest(dialog=name, viewport=(width, height)):
                    app = _make_test_app()
                    async with app.run_test(size=(width, height)) as pilot:
                        dialog = factory()
                        await pilot.app.push_screen(dialog)
                        await pilot.pause(0.25)
                        btn = pilot.app.screen.query_one(f"#{btn_id}", Button)
                        if _in_viewport(btn.region, height):
                            continue
                        await self._focus_trigger(pilot, focus_id)
                        targets = _scrollable_ancestors(btn)
                        before = {id(w): w.scroll_y for w, _, _ in targets}
                        await pilot.press("pagedown")
                        await pilot.pause(0.25)
                        moved = any(
                            w.scroll_y != before.get(id(w), 0)
                            for w, _, _ in targets
                        )
                        revealed = _in_viewport(btn.region, height)
                        self.assertTrue(
                            moved or revealed,
                            "PageDown 未滚动子页面内部容器且主按钮未进入视口 —— "
                            "PageDown/滚轮滚动被外层截断",
                        )

    # ---------- 5. Tab 不被外层截断 ----------

    async def test_tab_reaches_primary_button_and_reveals(self):
        """Tab 遍历能聚焦到底部主按钮，且聚焦后按钮被滚入视口（不被外层挡住）。"""
        for name, factory, btn_id, _focus_id, _observable in SUBPAGES:
            for (width, height) in VP_LOW:
                with self.subTest(dialog=name, viewport=(width, height)):
                    app = _make_test_app()
                    async with app.run_test(size=(width, height)) as pilot:
                        dialog = factory()
                        await pilot.app.push_screen(dialog)
                        await pilot.pause(0.25)
                        btn = pilot.app.screen.query_one(f"#{btn_id}", Button)
                        if _in_viewport(btn.region, height):
                            continue
                        # 聚焦首个可编辑字段后开始 Tab 遍历
                        first = (
                            pilot.app.screen.query(Input).first()
                            or pilot.app.screen.query(Select).first()
                            or btn
                        )
                        first.focus()
                        await pilot.pause(0.1)
                        reached = False
                        for _ in range(30):
                            await pilot.press("tab")
                            await pilot.pause(0.05)
                            if pilot.app.screen.focused is btn:
                                reached = True
                                break
                        self.assertTrue(reached, "Tab 遍历无法聚焦到底部主按钮")
                        await pilot.pause(0.2)
                        self.assertTrue(
                            _in_viewport(btn.region, height),
                            f"Tab 聚焦到主按钮但按钮仍在视口外（region={btn.region}, "
                            f"viewport_height={height}）—— 聚焦未触发滚入视口，滚动被外层截断",
                        )

    # ---------- 辅助 ----------

    async def _reveal(self, pilot, btn) -> bool:
        """把 btn 的真实可滚动祖先（overflow-y: auto/scroll）滚到底。

        跳过行内小滚动（如 .dialog-buttons 的 label 溢出，max_scroll_y<=2），
        只滚动真正的子页面纵向滚动容器。返回滚动是否实际发生（滚动能力生效）。

        注意：scroll_to(animate=False) 后 scroll_y 是异步传播的（需等一帧消息泵
        后才反映新偏移），故统一 pause 后再对比 before/after，避免时序假象。
        """
        targets = [
            (w, max_y)
            for w, max_y, overflow_y in _scrollable_ancestors(btn)
            if overflow_y in ("auto", "scroll")
        ]
        before = {id(w): w.scroll_y for w, _ in targets}
        for w, max_y in targets:
            w.scroll_to(y=max_y, animate=False)
        await pilot.pause(0.15)
        return any(w.scroll_y != before.get(id(w), 0) for w, _ in targets)

    async def _focus_trigger(self, pilot, focus_id) -> None:
        """聚焦用于 PageDown 的控件：优先显式 id，其次任意 Input/Select，最后主按钮。"""
        screen = pilot.app.screen
        if focus_id:
            w = screen.query_one(f"#{focus_id}")
        else:
            w = screen.query(Input).first() or screen.query(Select).first()
        if w is None:
            w = screen.query(Button).first()
        w.focus()
        await pilot.pause(0.1)

    async def _assert_focus_and_click(self, pilot, btn, dialog, observable) -> None:
        """断言主按钮可聚焦、可点击：focus 后 press 触发其注册处理器（可观测副作用）。"""
        self.assertTrue(btn.can_focus, "主按钮应为可聚焦控件")
        btn.focus()
        await pilot.pause(0.1)
        self.assertIs(pilot.app.screen.focused, btn, "focus() 后主按钮应成为当前焦点")
        btn.press()
        await pilot.pause(0.4)
        if observable == "screen":
            self.assertIsNot(
                pilot.app.screen,
                dialog,
                "点击主按钮后应产生可观测副作用（dismiss / MessageBox / 下一子页）",
            )
        else:  # result_label
            result = pilot.app.screen.query_one("#team_default_result")
            self.assertNotEqual(
                str(result.content).strip(),
                "",
                "点击【设为默认】后结果文本应更新（处理器已触发）",
            )


if __name__ == "__main__":
    unittest.main()
