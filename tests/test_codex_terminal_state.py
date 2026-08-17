"""Codex 终端状态识别回归（leader / 成员两侧共用一套原语）。

缺陷来源（2026-08-16 实机取样，真实 codex leader 窗口 mcp_team:lead）：

    › /compact
      [唤醒通知] Leader activation: a member reported a result....
      gpt-5.6-sol high · /tmp/tmpqx51vzza/workspace

``_classify_leader_terminal_output`` 整套按 Claude TUI 写成 ——
``_is_claude_ready_prompt`` 要求 ``›``/``❯`` **下面那行**是 Claude 的模式行
（manual mode / tokens / brewed for…），而 codex 的 footer 是
``<model> [effort] · <cwd>``、快捷键提示行或 ``NN% context left``，一条都不沾；
兜底的 idle_markers 同样只有 Claude 词，成员侧 idle_markers 更是只有 ``❯``
没有 codex 的 ``›``（U+203A）。

后果不是"状态显示不好看"：``_leader_terminal_is_idle`` 是**四条注入链路的共同
前置门**（超时唤醒 / 回报唤醒 / 授权唤醒 / 巡检兜底补投），codex leader 恒判
unknown → 一条都发不出去 → leader_sleep 之后再也醒不过来。

本文件锁三件事：
  A. codex 静止帧必须判 idle（leader 与成员两侧同时成立）；
  B. **安全网**：codex 活动帧必须仍判 busy —— 修 idle 最大的风险是反向制造
     fake-idle（monitor 会 mark_idle_done 伪造任务完成）；
  C. codex 的 ``■`` 系统提示（``■ '/compact' is disabled while a task is in
     progress.``）不得把成员钉成永久 busy —— 它含 "in progress" 且行首是 ■，
     与任务清单 ``◼`` 是同一类同形碰撞。

码位事实（踩过）：codex 用 ``■`` U+25A0，Claude 用 ``◼`` U+25FC，不是同一个
字符；``■`` 只在**同行还带耗时计数**时才算活动标记，否则它是普通提示前缀。
"""
import unittest

import mult_agent_mcp as mcp

# 实机 footer（取自真实 codex 窗口）与其它两种常见 footer 形态
CODEX_FOOTER = "  gpt-5.6-sol high · /tmp/tmpqx51vzza/workspace"
CODEX_HOTKEYS = "  ⏎ send   ⌃J newline   ⌃T transcript   ⌃C quit"
CODEX_CONTEXT = "  87% context left"


def _cap(*lines: str) -> str:
    return "\n".join(lines)


class CodexIdleRecognitionTests(unittest.TestCase):
    """A. codex 静止帧 → idle（两侧同时成立，绝不各写一套）。"""

    def _both(self, capture: str) -> tuple[str, str]:
        return (
            mcp._classify_leader_terminal_output(capture),
            mcp._classify_terminal_output(capture),
        )

    def test_codex_prompt_with_model_cwd_footer_is_idle(self):
        """实机形态：``›`` + ``<model> <effort> · <cwd>``。"""
        leader, member = self._both(_cap("上一轮回复已完成。", "›", "", CODEX_FOOTER))
        self.assertEqual(leader, "idle")
        self.assertEqual(member, "idle")

    def test_codex_prompt_with_hotkey_footer_is_idle(self):
        leader, member = self._both(_cap("上文", "›", CODEX_HOTKEYS))
        self.assertEqual(leader, "idle")
        self.assertEqual(member, "idle")

    def test_codex_prompt_with_context_left_footer_is_idle(self):
        leader, member = self._both(_cap("上文", "›", CODEX_CONTEXT))
        self.assertEqual(leader, "idle")
        self.assertEqual(member, "idle")

    def test_codex_prompt_with_typed_text_is_idle(self):
        """输入框里有占位/已键入文本同样是静止（未提交 ≠ 正在跑）。"""
        leader, member = self._both(
            _cap("some prior output", "› Implement {feature}", "", CODEX_FOOTER)
        )
        self.assertEqual(leader, "idle")
        self.assertEqual(member, "idle")

    def test_leader_terminal_is_idle_gate_opens_for_codex(self):
        """闸门本体：codex 静止帧必须让 _classify → idle（注入链路的前置条件）。"""
        self.assertEqual(
            mcp._classify_leader_terminal_output(_cap("done", "›", CODEX_FOOTER)), "idle"
        )


class CodexBusySafetyNetTests(unittest.TestCase):
    """B. 安全网：codex 活动帧绝不能被 footer 洗成 idle（fake-idle = 伪造完成）。"""

    def _both(self, capture: str) -> tuple[str, str]:
        return (
            mcp._classify_leader_terminal_output(capture),
            mcp._classify_terminal_output(capture),
        )

    def test_codex_working_with_esc_to_interrupt_is_busy(self):
        leader, member = self._both(
            _cap("上文", "■ Working (12s • esc to interrupt)", "›", CODEX_FOOTER)
        )
        self.assertEqual(leader, "busy")
        self.assertEqual(member, "busy")

    def test_codex_working_without_esc_is_busy(self):
        leader, member = self._both(_cap("上文", "◦ Working (7s)", "›", CODEX_FOOTER))
        self.assertEqual(leader, "busy")
        self.assertEqual(member, "busy")

    def test_codex_thinking_with_elapsed_is_busy(self):
        """``■ Thinking (3s)`` —— 修 idle 之前 leader 侧判 unknown（只看末行），
        修完若不同步加固就会变成 idle → 往正在思考的 leader 注入。"""
        leader, member = self._both(_cap("上文", "■ Thinking (3s)", "›", CODEX_FOOTER))
        self.assertEqual(leader, "busy")
        self.assertEqual(member, "busy")

    def test_codex_quota_error_frame_is_not_idle(self):
        """中转站配额行同样含 ``·``，绝不能被当成 codex footer 洗成静止。"""
        capture = _cap(
            "Please run /login·API Error:403 用户额度不足,剩余额度:¥0.00000000",
            "›",
            CODEX_FOOTER,
        )
        leader, member = self._both(capture)
        self.assertEqual(leader, "quota")
        self.assertEqual(member, "quota")

    def test_codex_approval_frame_is_approval(self):
        leader, member = self._both(
            _cap("Allow command?", "❯ 1. Yes", "  2. No", CODEX_FOOTER)
        )
        self.assertEqual(leader, "approval")
        self.assertEqual(member, "approval")

    def test_quota_error_line_alone_is_not_a_status_line(self):
        """单元级：配额行不得被 _is_codex_status_line 认作 footer。"""
        self.assertFalse(
            mcp._is_codex_status_line(
                "Please run /login·API Error:403 用户额度不足,剩余额度:¥0.00"
            )
        )
        self.assertTrue(mcp._is_codex_status_line(CODEX_FOOTER))
        self.assertTrue(mcp._is_codex_status_line(CODEX_HOTKEYS))


class CodexNoticeBusyCollisionTests(unittest.TestCase):
    """C. ``■ '<cmd>' is disabled …`` 系统提示不得把成员钉成永久 busy。"""

    NOTICE = "■ '/compact' is disabled while a task is in progress."

    def test_codex_disabled_notice_does_not_pin_member_busy(self):
        capture = _cap(self.NOTICE, "›", CODEX_FOOTER)
        self.assertEqual(mcp._classify_terminal_output(capture), "idle")
        self.assertEqual(mcp._classify_leader_terminal_output(capture), "idle")

    def test_codex_disabled_notice_does_not_mask_quota(self):
        """提示行被剔除后，同屏的配额错误必须仍然定案（busy 先于 quota 判定）。"""
        capture = _cap(
            self.NOTICE,
            "✗ Error: 429 insufficient_quota",
            "  You exceeded your current quota, please check your plan and billing details.",
            "›",
            CODEX_FOOTER,
        )
        self.assertEqual(mcp._classify_terminal_output(capture), "quota")

    def test_notice_regex_does_not_swallow_real_activity(self):
        """安全网反例：带耗时计数的 ``■`` 活动行不是提示行，必须仍算 busy。"""
        self.assertEqual(mcp._drop_codex_notice_lines(["■ Working (5s)"]), ["■ Working (5s)"])
        self.assertEqual(mcp._drop_codex_notice_lines([self.NOTICE]), [])
        capture = _cap(self.NOTICE, "■ Working (5s • esc to interrupt)", "›", CODEX_FOOTER)
        self.assertEqual(mcp._classify_terminal_output(capture), "busy")


class ClaudeSideNoRegressionTests(unittest.TestCase):
    """Claude 侧既有语义不得因为"顺手支持 codex"而漂移。"""

    def test_claude_idle_frames_unchanged(self):
        for capture in (
            "✻ Brewed for 5s\n❯\n⏸ manual mode on",
            "回复完毕\n❯\n────────────\n  ⏸ manual mode on · ? for shortcuts",
        ):
            self.assertEqual(mcp._classify_leader_terminal_output(capture), "idle")
            self.assertEqual(mcp._classify_terminal_output(capture), "idle")

    def test_claude_live_tool_still_busy(self):
        capture = "✢ Waddling… (42s · ↓ 5.3k tokens)\n❯\n⏸ manual mode on"
        self.assertEqual(mcp._classify_leader_terminal_output(capture), "busy")
        self.assertEqual(mcp._classify_terminal_output(capture), "busy")

    def test_bare_stop_glyph_without_task_list_still_busy_on_member(self):
        """上一轮的安全网不得被本次改动削弱：无清单证据的裸 ◼ 仍是 busy。"""
        self.assertEqual(mcp._classify_terminal_output("◼ 处理中\n❯\n⏸ manual mode on"), "busy")

    def test_shell_prompt_still_dead(self):
        self.assertEqual(mcp._classify_leader_terminal_output("zwc@host:~/w$ "), "dead")
        self.assertEqual(mcp._classify_terminal_output("zwc@host:~/w$ "), "dead")


if __name__ == "__main__":
    unittest.main()
