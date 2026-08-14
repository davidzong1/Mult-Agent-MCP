"""
tester-claude 验收/复现测试 —— TUI/MCP 启动链路 system prompt 来源核验
======================================================================

可重复判据（对应当前派单任务「TUI 启动成员终端时 system prompt 是否落到
mult_agent_mcp.py 默认模板」）：

  A. 系统通道（Claude 真实 system prompt = ``--append-system-prompt-file`` 指向的
     文件内容）——**已正确来自 prompts/*.ts**：
       TUI 新建 leader / TUI 新建 member / TUI 恢复 member / MCP 基线 member
     四条路径的 append 文件内容必须 == prompt_registry 从 prompts/*.ts 渲染的文本
     （memberSystemPrompt / leaderSystemPrompt），且 != mult_agent_mcp.py 默认
     内联模板、!= 默认占位文件。本组是**验收（应绿）**，提供可核验证据。

  B. 用户通道（首启/恢复/派单/leader 首启，经 send-keys / argv 注入）——应按
     docs/prompt_template_runtime_design.md §8 接线 prompts/*.ts 权威源：
     TDD 判据（注入临时 prompts/ 并在 user 通道函数放入唯一标记 → 运行时消息必须
     反映该标记）证明消息由 TS 渲染产生。接线未完成时本组红（当前基线含并行
     coder 接线进度，运行时可观测）。

  C. 真实 tmux 黑盒：用 fake claude wrapper 作为成员 agent 走真实 TUI 启动，捕获
     真实 spawn argv 中 ``--append-system-prompt-file`` 指向的文件内容 == TS 渲染，
     提供「真实终端内启动后的 prompt 内容」证据。

隔离：data_layer.set_data_file + mcp 全局路径重定向（镜像
test_prompt_template_integration / test_prompt_identity_system_layer）；黑盒组仅写
临时数据文件 + 真实 tmux 会话（实例级唯一名，teardown kill），不触碰真实 home。

边界：只写本文件；不改主实现（mult_agent_mcp / common / tui / prompts）。
交叉文件说明：当前工作树有并行 coder 正在接线 user 通道（common/prompt_registry.py、
mult_agent_mcp.py、tui/tui_screens.py 未提交改动）——本文件只读这些模块的公开
行为，不修改它们。
"""

import json
import os
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

import mult_agent_mcp as mcp
import tui.tui_screens as tui_screens
from common import data_layer
from common import prompt_registry as pr
from common import prompt_template as pt

# 默认模板（mult_agent_mcp.py 内联文本）独有标记；TS 模板无这些字面量
DEFAULT_PLACEHOLDER_MARK = "身份文件占位默认值"
DEFAULT_LEADER_MARK = (
    "开始后先调用 leader_list_team 查看成员，再用 leader_assign_subtask、"
    "leader_broadcast 等 leader_* 工具分配任务。"
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _ts_defined_functions(ts_name: str) -> list[str]:
    parsed = pt.load_parsed(ts_name)
    return sorted(parsed.functions.keys())


# =====================================================================
# 隔离基类（确定性单测）
# =====================================================================
class _IsolatedTuiMCP(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_globals = {
            "PROJECT_DIR": mcp.PROJECT_DIR,
            "MCP_HOME": mcp.MCP_HOME,
            "DATA_FILE": mcp.DATA_FILE,
            "TEAM_WORKSPACES_DIR": mcp.TEAM_WORKSPACES_DIR,
            "SHARE_CONTEXT_DIR": mcp.SHARE_CONTEXT_DIR,
            "SHARE_WORKSPACE_DIR": mcp.SHARE_WORKSPACE_DIR,
        }
        self.old_data_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        self.old_env = {
            key: os.environ.get(key)
            for key in ("MULT_AGENT_MCP_WORKSPACE", "CODEX_WORKSPACE",
                        "ORIGINAL_CWD", "INIT_CWD", "PWD",
                        "MULTI_AGENT_MCP_PROMPTS_DIR")
        }
        project = self.root / "project"
        project.mkdir()
        mcp.PROJECT_DIR = str(project)
        mcp.MCP_HOME = str(project / ".mult_agent_mcp")
        mcp.DATA_FILE = str(project / ".mult_agent_mcp" / "teams_data.json")
        data_layer.set_data_file(mcp.DATA_FILE)
        mcp.TEAM_WORKSPACES_DIR = str(project / ".team_workspaces")
        mcp.SHARE_CONTEXT_DIR = str(project / ".mult_agent_mcp" / "contexts")
        mcp.SHARE_WORKSPACE_DIR = str(project / "share_work_space")
        for key in self.old_env:
            os.environ.pop(key, None)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.context = self.root / "ctx"
        self.context.mkdir()
        # 隔离加固（2026-08-14）：TUI 成员窗口存在性判定 member_window_state 经
        # common.tmux_utils 真实 tmux_run（非本测试 mock 的 ts._tmux_run/_tmux），
        # 固定会话名会与真实 tmux 会话状态碰撞（见 test_tui_member_spawn_ts_source
        # 修复注释）。改用实例级唯一会话名 → 真实 has-session 必 rc!=0 → 判定
        # absent → 创建，不依赖外部 tmux 状态。
        self.session = f"mcp_iso_{uuid.uuid4().hex[:6]}"

    def tearDown(self):
        for name, val in self.old_globals.items():
            setattr(mcp, name, val)
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        data_layer._DATA_FILE_OVERRIDE = self.old_data_override
        self.tmp.cleanup()

    def _save_team(self, *, members, leader="lead"):
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(self.workspace),
                    "context_dir": str(self.context),
                    "leader": leader,
                    "leader_type": "tmux",
                    "default_agent": "claude",
                    "monitor_enabled": False,
                    "members": {
                        leader: {"role": "leader", "agent": "claude"},
                        **members,
                    },
                }
            }
        })

    def _team_data(self):
        return {"teams": {"team": {
            "workspace_dir": str(self.workspace),
            "context_dir": str(self.context),
            "leader": "lead", "leader_type": "tmux",
            "default_agent": "claude", "monitor_enabled": False,
            "members": {
                "lead": {"role": "leader", "agent": "claude"},
                "coder_a": {"role": "coder", "agent": "claude"},
            },
        }}}

    # ---- TUI launch 捕获（mock _tmux_run） ----
    def _tui_launch(self):
        data = self._team_data()
        tmux_calls = []

        def fake_tmux(cmd, timeout=10):
            tmux_calls.append(list(cmd))
            c0 = cmd[0] if isinstance(cmd, list) else str(cmd)
            if c0 == "-V":
                return 0, "", ""
            if c0 == "has-session":
                return 1, "", ""
            return 0, "", ""

        with mock.patch.object(tui_screens, "_tmux_run", side_effect=fake_tmux), \
             mock.patch.object(tui_screens, "load_data", return_value=data), \
             mock.patch.object(tui_screens, "save_data", return_value=None), \
             mock.patch.object(tui_screens, "_tmux_session", return_value=self.session), \
             mock.patch.object(tui_screens, "_leader_terminal_restart_blocked", return_value=False), \
             mock.patch.object(tui_screens, "_record_leader_reentry", return_value=None), \
             mock.patch.object(tui_screens, "write_claude_mcp", return_value=""), \
             mock.patch.object(tui_screens, "configure_codex_mcp", return_value=(True, "")), \
             mock.patch.object(tui_screens, "configure_claude_mcp", return_value=(True, "")), \
             mock.patch.object(tui_screens, "write_claude_permissions", return_value=""), \
             mock.patch.object(tui_screens, "_remember_member_window_id", return_value=""), \
             mock.patch.object(tui_screens, "_inject_claude_leader_prompt", return_value=(0, "")):
            ok, msg = tui_screens.launch_terminals("team")
        return ok, msg, tmux_calls

    @staticmethod
    def _append_content_from_cmd(cmd):
        if "--append-system-prompt-file" not in cmd:
            return None
        idx = cmd.index("--append-system-prompt-file")
        path = cmd[idx + 1]
        return Path(path).read_text(encoding="utf-8")

    # ---- MCP 基线捕获 ----
    # MCP 路径窗口状态经 mcp._tmux mock（run_tmux=_tmux 注入），已隔离；且 session
    # 名须为 "mcp_<team>" 供 _resolve_team_name_from_session 解析团队，故保持固定。
    def _mcp_member_spawn(self, session="mcp_team"):
        calls = []

        def fake_tmux(cmd, timeout=10):
            calls.append(list(cmd))
            c0 = cmd[0] if isinstance(cmd, list) else str(cmd)
            if c0 == "has-session":
                return 0, "", ""
            if c0 == "list-windows":
                return 0, "$1\t1000\t@1\t__base", ""
            if c0 == "-V":
                return 0, "", ""
            return 0, "", ""

        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux), \
             mock.patch.object(
                 mcp, "_write_claude_permissions",
                 return_value=str(self.workspace / ".claude" / "settings.json")), \
             mock.patch.object(mcp, "_send_keys", return_value=(0, "")), \
             mock.patch.object(mcp, "_start_team_monitor", return_value=None):
            rc = mcp._tmux_spawn_member(session, "coder_a", "claude", str(self.workspace))
        spawn_cmds = [c for c in calls if c[0] in {"new-session", "new-window"}]
        return rc, spawn_cmds


# =====================================================================
# A. 系统通道验收（应绿）：TUI/MCP 各路径 append 文件内容 == prompts/*.ts 渲染
# =====================================================================
class SystemPromptSourceTests(_IsolatedTuiMCP):
    """四条路径的真实 system prompt（append 文件内容）必须来自 prompts/*.ts。"""

    def _expected_member(self):
        return pr.render_member_identity("team", "coder_a")

    def _expected_leader(self):
        return pr._render_leader_system("team")

    def test_tui_launch_leader_system_prompt_comes_from_leader_ts(self):
        self._save_team(members={"coder_a": {"role": "coder", "agent": "claude"}})
        ok, msg, calls = self._tui_launch()
        self.assertTrue(ok, f"launch failed: {msg}")
        lead_cmd = next((c for c in calls if c and c[0] == "new-session"), None)
        self.assertIsNotNone(lead_cmd, "未捕获 TUI leader new-session 命令")
        content = self._append_content_from_cmd(lead_cmd)
        self.assertIsNotNone(content, "leader 启动命令缺 --append-system-prompt-file")
        # 必须 == leader.ts leaderSystemPrompt 渲染
        self.assertEqual(content, self._expected_leader(),
                         "TUI leader system prompt 应来自 prompts/leader.ts 渲染")
        # 不得是 mult_agent_mcp 默认模板 / 占位
        self.assertNotIn(DEFAULT_LEADER_MARK, content,
                         "TUI leader system prompt 不得落 mult_agent_mcp 默认模板")
        self.assertNotIn(DEFAULT_PLACEHOLDER_MARK, content)

    def test_tui_launch_member_system_prompt_comes_from_members_ts(self):
        self._save_team(members={"coder_a": {"role": "coder", "agent": "claude"}})
        ok, msg, calls = self._tui_launch()
        self.assertTrue(ok, f"launch failed: {msg}")
        mem_cmd = next((c for c in calls if c and c[0] == "new-window"), None)
        self.assertIsNotNone(mem_cmd, "未捕获 TUI member new-window 命令")
        content = self._append_content_from_cmd(mem_cmd)
        self.assertIsNotNone(content, "member 启动命令缺 --append-system-prompt-file")
        self.assertEqual(content, self._expected_member(),
                         "TUI member system prompt 应来自 prompts/members.ts 渲染")
        self.assertNotIn(DEFAULT_PLACEHOLDER_MARK, content)

    def test_tui_recovery_member_system_prompt_comes_from_members_ts(self):
        """TUI 自动恢复路径与新建共用 pr.claude_identity_file → append 内容同 TS。"""
        self._save_team(members={"coder_a": {"role": "coder", "agent": "claude"}})
        identity_path = pr.claude_identity_file("team", "coder_a")  # 恢复路径同一调用
        content = Path(identity_path).read_text(encoding="utf-8")
        self.assertEqual(content, self._expected_member(),
                         "TUI 恢复 member system prompt 应来自 members.ts 渲染")
        self.assertNotIn(DEFAULT_PLACEHOLDER_MARK, content)

    def test_mcp_baseline_member_system_prompt_comes_from_members_ts(self):
        self._save_team(members={"coder_a": {"role": "coder", "agent": "claude"}})
        rc, spawn = self._mcp_member_spawn()
        self.assertEqual(rc[0], 0)
        self.assertTrue(spawn, "未捕获 MCP member spawn 命令")
        content = self._append_content_from_cmd(spawn[0])
        self.assertIsNotNone(content, "MCP member 启动命令缺 --append-system-prompt-file")
        self.assertEqual(content, self._expected_member(),
                         "MCP 基线 member system prompt 应来自 members.ts 渲染")
        self.assertNotIn(DEFAULT_PLACEHOLDER_MARK, content)

    def test_leader_identity_file_leader_true_is_leader_not_member(self):
        """leader=True 时 claude_identity_file 必须渲染 leader.ts，不得误用成员身份。"""
        self._save_team(members={"coder_a": {"role": "coder", "agent": "claude"}})
        path = pr.claude_identity_file("team", "lead", leader=True)
        content = Path(path).read_text(encoding="utf-8")
        self.assertEqual(content, self._expected_leader())
        self.assertIn("leader", content)
        self.assertNotIn("你是 Multi-Agent MCP 团队 'team' 的成员。", content)


# =====================================================================
# B. 用户通道接线验收（TDD 判据：编辑 prompts/*.ts 的 user 通道函数 → 运行时反映）
# =====================================================================
class UserChannelWiringTests(_IsolatedTuiMCP):
    """用户通道（initial/recovery/task/leader-initial）应接线 prompts/*.ts 权威源。

    判据（docs/prompt_template_runtime_design.md §8）：prompts/*.ts 为运行时可编辑
    权威源——注入临时 prompts/ 并在 user 通道函数中放入唯一标记后，运行时消息必须
    反映该标记（说明消息由 TS 渲染产生，而非 Python 硬编码）。渲染失败回退 Python
    内联文本属合法（A4），但回退必须可观测（本组不覆盖回退分支）。
    """

    def setUp(self):
        super().setUp()
        self._save_team(members={"coder_a": {"role": "coder", "agent": "claude"}})

    def _inject_modified_prompts(self) -> Path:
        """构造临时 prompts/：user 通道函数注入唯一标记（system 通道保持合法）。"""
        tmp_dir = Path(tempfile.mkdtemp(prefix="prompts-wire-"))
        (tmp_dir / "members.ts").write_text(f"""
export interface MemberPromptVars {{ teamName: string; memberName: string; role: string; agent: string; mode: string; leader: string; leaderType: string; teamDir: string; shareDir: string; task: string; recoverySection: string; }}
export function memberSystemPrompt(v: MemberPromptVars): string {{ return `sys-member {{v.teamName}}`; }}
export function codexAgentsSection(v: MemberPromptVars): string {{ return `codex {{v.teamName}}`; }}
export function memberInitialContext(v: MemberPromptVars): string {{ return `[成员上下文] GAP-INITIAL-MARKER`; }}
export function memberRecoveryContext(v: MemberPromptVars): string {{ return `[恢复通知] GAP-RECOVERY-MARKER`; }}
export function memberTaskPayload(v: MemberPromptVars): string {{ return `[子任务] GAP-TASK-MARKER`; }}
""")
        (tmp_dir / "leader.ts").write_text(f"""
export interface LeaderPromptVars {{ teamName: string; leaderMemberName: string; leaderRole: string; leaderAgent: string; defaultAgent: string; teammates: string; task: string; teamDir: string; shareDir: string; recoverySection: string; }}
export function leaderSystemPrompt(v: LeaderPromptVars): string {{ return `sys-leader {{v.teamName}}`; }}
export function leaderInitialContext(v: LeaderPromptVars): string {{ return `你是 leader。\\nGAP-LEADER-INITIAL-MARKER`; }}
""")
        return tmp_dir

    def test_ts_templates_define_initial_recovery_task_functions(self):
        """接线前提：TS 已定义这些通道函数（存在可接线目标）。"""
        self.assertEqual(
            _ts_defined_functions("members"),
            ["codexAgentsSection", "memberInitialContext", "memberRecoveryContext",
             "memberSystemPrompt", "memberTaskPayload"])
        self.assertEqual(
            _ts_defined_functions("leader"),
            ["leaderInitialContext", "leaderSystemPrompt"])

    def _wired_texts(self, tmp_dir) -> dict:
        data = self._team_data()["teams"]["team"]
        from common import tmux_utils as tu
        with mock.patch.object(pt, "_prompts_dir", return_value=tmp_dir):
            return {
                "initial": mcp._build_member_initial_context("team", "coder_a"),
                "task": mcp._build_member_task_payload("do x")[0],
                "recovery": mcp._build_recovery_context("team", "coder_a"),
                "tui_recovery": tui_screens._build_tui_recovery_message(
                    data, "coder_a", data["members"]["coder_a"], "team"),
                "leader_init": mcp._leader_system_prompt("team"),
                "tui_leader": tui_screens._leader_system_prompt("team"),
                "tmux_utils_leader": tu.leader_system_prompt("team"),
            }

    def test_member_initial_context_wired_to_member_initial_ts(self):
        texts = self._wired_texts(self._inject_modified_prompts())
        self.assertIn("GAP-INITIAL-MARKER", texts["initial"],
                      "_build_member_initial_context 未接 members.ts memberInitialContext")

    def test_member_task_payload_wired_to_task_ts(self):
        texts = self._wired_texts(self._inject_modified_prompts())
        self.assertIn("GAP-TASK-MARKER", texts["task"],
                      "_build_member_task_payload 未接 members.ts memberTaskPayload")

    def test_recovery_context_wired_to_recovery_ts(self):
        texts = self._wired_texts(self._inject_modified_prompts())
        self.assertIn("GAP-RECOVERY-MARKER", texts["recovery"],
                      "_build_recovery_context 未接 members.ts memberRecoveryContext")

    def test_tui_recovery_message_wired_to_recovery_ts(self):
        texts = self._wired_texts(self._inject_modified_prompts())
        self.assertIn("GAP-RECOVERY-MARKER", texts["tui_recovery"],
                      "TUI _build_tui_recovery_message 未接 members.ts memberRecoveryContext")

    def test_leader_initial_wired_to_leader_initial_ts(self):
        texts = self._wired_texts(self._inject_modified_prompts())
        self.assertIn("GAP-LEADER-INITIAL-MARKER", texts["leader_init"],
                      "_leader_system_prompt 未接 leader.ts leaderInitialContext")

    def test_tui_leader_wired_to_single_source(self):
        """TUI leader 首启应收敛到 mcp 单一源（消除 Python 副本漂移）。"""
        from mult_agent_mcp import _leader_system_prompt as mcp_lead
        from tui import tui_screens as ts_mod
        texts = self._wired_texts(self._inject_modified_prompts())
        self.assertIn("GAP-LEADER-INITIAL-MARKER", texts["tui_leader"],
                      "TUI _leader_system_prompt 未收敛到 leader.ts leaderInitialContext 源")
        self.assertEqual(ts_mod._leader_system_prompt("team"), mcp_lead("team"),
                         "TUI leader 首启应委托 mult_agent_mcp 单一来源")

    def test_tmux_utils_leader_wired_to_leader_initial_ts(self):
        texts = self._wired_texts(self._inject_modified_prompts())
        self.assertIn("GAP-LEADER-INITIAL-MARKER", texts["tmux_utils_leader"],
                      "tmux_utils.leader_system_prompt 未接 leader.ts leaderInitialContext")

    def test_tui_launch_does_not_send_member_initial_context(self):
        """功能性差异：MCP 启动后向成员 send-keys 首启上下文，TUI 不发送（供 leader 裁决）。"""
        ok, _msg, calls = self._tui_launch()
        self.assertTrue(ok)
        member_send_keys = [c for c in calls if c and c[0] == "send-keys" and "coder_a" in c]
        self.assertEqual(len(member_send_keys), 0,
                         "当前 TUI 启动不注入成员首启上下文（MCP 基线会注入）")


# =====================================================================
# C. 真实 tmux 黑盒：真实启动后 append 文件内容 == TS 渲染
# =====================================================================
_WRAPPER_SRC = r'''#!/bin/bash
# claude-sim: 捕获真实 spawn argv 与 --append-system-prompt-file 文件内容
OUT="__OUT__"
APPEND=""
prev=""
for a in "$@"; do
  if [ "$prev" = "--append-system-prompt-file" ]; then APPEND="$a"; fi
  prev="$a"
done
{
  echo "full_argv=$*"
  echo "append_file=${APPEND}"
  if [ -n "$APPEND" ]; then
    echo "---append-content-begin---"
    cat "$APPEND"
    echo "---append-content-end---"
  fi
} > "$OUT"
sleep 60
'''

_CODEX_WRAPPER_SRC = r'''#!/bin/bash
# codex-sim: 捕获真实 spawn argv（含末尾位置参数 prompt）——不执行任何 codex 行为
OUT="__OUT__"
last=""
for a in "$@"; do last="$a"; done
{
  echo "argc=$#"
  echo "last_argv=${last}"
  echo "full_argv=$*"
} > "$OUT"
sleep 60
'''


class RealTmuxLaunchBlackboxTests(unittest.TestCase):
    """真实 TUI + 真实 tmux：fake claude wrapper 捕获启动后注入的 system prompt。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.uid = uuid.uuid4().hex[:8]
        self.session = f"mcp_bb_{self.uid}"
        self.team_name = f"team_{self.uid}"
        self.data_file = self.root / "teams_data.json"
        data_layer.set_data_file(self.data_file)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        subprocess.run(["tmux", "kill-session", "-t", self.session],
                       capture_output=True, text=True)
        data_layer._DATA_FILE_OVERRIDE = None
        try:
            self.tmp.cleanup()
        except Exception:
            pass

    def _make_wrapper(self, tag: str) -> tuple[Path, Path]:
        out = self.root / f"out-{tag}.txt"
        p = self.root / f"claude-sim-{tag}-{self.uid}.sh"  # 名字含 claude → agent_type=claude
        p.write_text(_WRAPPER_SRC.replace("__OUT__", str(out)))
        p.chmod(0o755)
        return p, out

    def _wait_out(self, out: Path, timeout: float = 15.0) -> str:
        end = time.time() + timeout
        while time.time() < end and not out.exists():
            time.sleep(0.2)
        return out.read_text(encoding="utf-8") if out.exists() else ""

    def test_real_tui_launch_append_content_matches_ts_render(self):
        ws = self.root / "ws"
        ws.mkdir(parents=True, exist_ok=True)
        leader_w, leader_out = self._make_wrapper("leader")
        member_w, member_out = self._make_wrapper("member")
        data = {"teams": {self.team_name: {
            "workspace_dir": str(ws), "context_dir": str(ws),
            "leader": "lead", "leader_type": "tmux", "default_agent": "claude",
            "monitor_enabled": False,
            "members": {
                "lead": {"role": "leader", "agent": str(leader_w)},
                "coder_a": {"role": "coder", "agent": str(member_w)},
            },
        }}}
        self.data_file.write_text(json.dumps(data, ensure_ascii=False))
        # 预置 TS 渲染期望值（leader 复用当前 leader.ts；member 用当前 members.ts）
        from common import prompt_registry as pr_
        expected_leader = pr_._render_leader_system(self.team_name)
        expected_member = pr_.render_member_identity(self.team_name, "coder_a")

        with mock.patch.object(tui_screens, "_tmux_session", return_value=self.session), \
             mock.patch.object(tui_screens, "_leader_terminal_restart_blocked", return_value=False), \
             mock.patch.object(tui_screens, "_record_leader_reentry", return_value=None), \
             mock.patch.object(tui_screens, "write_claude_mcp", return_value=""), \
             mock.patch.object(tui_screens, "configure_codex_mcp", return_value=(True, "")), \
             mock.patch.object(tui_screens, "configure_claude_mcp", return_value=(True, "")), \
             mock.patch.object(tui_screens, "write_claude_permissions", return_value=""), \
             mock.patch.object(tui_screens, "_inject_claude_leader_prompt", return_value=(0, "")):
            ok, msg = tui_screens.launch_terminals(self.team_name)
        self.assertTrue(ok, f"真实 TUI launch failed: {msg}")

        leader_cap = self._wait_out(leader_out)
        member_cap = self._wait_out(member_out)
        self.assertIn("append_file=", leader_cap, "leader wrapper 未捕获 append 文件")
        self.assertIn("append_file=", member_cap, "member wrapper 未捕获 append 文件")

        leader_content = leader_cap.split("---append-content-begin---", 1)[-1] \
                                  .split("---append-content-end---", 1)[0].strip("\n")
        member_content = member_cap.split("---append-content-begin---", 1)[-1] \
                                   .split("---append-content-end---", 1)[0].strip("\n")
        # 真实终端内启动后的 system prompt 必须 == TS 渲染
        self.assertEqual(leader_content, expected_leader,
                         "真实 TUI leader 终端 system prompt 应来自 prompts/leader.ts")
        self.assertEqual(member_content, expected_member,
                         "真实 TUI member 终端 system prompt 应来自 prompts/members.ts")
        self.assertNotIn(DEFAULT_PLACEHOLDER_MARK, leader_content)
        self.assertNotIn(DEFAULT_PLACEHOLDER_MARK, member_content)


class RealTmuxCodexLeaderBlackboxTests(RealTmuxLaunchBlackboxTests):
    """真实 TUI + 真实 tmux：codex leader argv 初始内容来自 leaderInitialContext。

    复用基类隔离/包装工具；leader 用名字含 codex 的 fake wrapper（agent_type=codex）。
    校验：
      - codex leader argv 末尾位置参数 == mult_agent_mcp._leader_system_prompt(team)
        （该函数接线 prompts/leader.ts leaderInitialContext，@channel initial）；
      - 该 prompt 不含旧 Python 默认标记（leader_assign_subtask/leader_broadcast 措辞
        与身份文件占位）——证明 argv 初始内容来自 TS 渲染而非内建回退；
      - 同团队 claude member 的 append system 内容仍 == members.ts 渲染（混合团队）。
    """

    def test_real_tui_codex_leader_argv_initial_from_leader_ts(self):
        ws = self.root / "ws"
        ws.mkdir(parents=True, exist_ok=True)
        codex_w, codex_out = self._make_wrapper("cxleader")  # 复用命名槽：tag 含 codex → 见下
        # 覆盖命名：确保 wrapper 文件名含 codex（agent_type=codex），而非 claude
        codex_w.unlink()
        codex_w = self.root / f"codex-sim-{self.uid}.sh"
        codex_w.write_text(_CODEX_WRAPPER_SRC.replace("__OUT__", str(codex_out)))
        codex_w.chmod(0o755)
        member_w, member_out = self._make_wrapper("member")

        data = {"teams": {self.team_name: {
            "workspace_dir": str(ws), "context_dir": str(ws),
            "leader": "lead", "leader_type": "tmux", "default_agent": "claude",
            "monitor_enabled": False,
            "members": {
                "lead": {"role": "leader", "agent": str(codex_w)},
                "coder_a": {"role": "coder", "agent": str(member_w)},
            },
        }}}
        self.data_file.write_text(json.dumps(data, ensure_ascii=False))
        expected_leader_initial = mcp._leader_system_prompt(self.team_name)
        expected_member = pr.render_member_identity(self.team_name, "coder_a")

        with mock.patch.object(tui_screens, "_tmux_session", return_value=self.session), \
             mock.patch.object(tui_screens, "_leader_terminal_restart_blocked", return_value=False), \
             mock.patch.object(tui_screens, "_record_leader_reentry", return_value=None), \
             mock.patch.object(tui_screens, "write_claude_mcp", return_value=""), \
             mock.patch.object(tui_screens, "configure_codex_mcp", return_value=(True, "")), \
             mock.patch.object(tui_screens, "configure_claude_mcp", return_value=(True, "")), \
             mock.patch.object(tui_screens, "write_claude_permissions", return_value=""), \
             mock.patch.object(tui_screens, "_inject_claude_leader_prompt", return_value=(0, "")):
            ok, msg = tui_screens.launch_terminals(self.team_name)
        self.assertTrue(ok, f"真实 TUI launch failed: {msg}")

        codex_cap = self._wait_out(codex_out)
        self.assertIn("last_argv=", codex_cap, "codex wrapper 未捕获 argv")
        # 末尾位置参数 prompt 含换行：取 last_argv= 与 full_argv= 之间整段，去掉 echo 自带换行
        last_argv = codex_cap.split("last_argv=", 1)[1].split("full_argv=", 1)[0].rstrip("\n")
        # codex leader argv 末尾位置参数 == leaderInitialContext 渲染（@channel initial）
        self.assertEqual(last_argv, expected_leader_initial,
                         "codex leader argv 初始内容应来自 prompts/leader.ts leaderInitialContext")
        # 无旧 Python 标记（leader_assign_subtask/leader_broadcast 措辞 + 身份文件占位）
        self.assertNotIn(DEFAULT_LEADER_MARK, last_argv,
                         "codex leader argv 不得落 mult_agent_mcp 旧 Python 默认模板")
        self.assertNotIn(DEFAULT_PLACEHOLDER_MARK, last_argv)

        # 混合团队：claude member append system 仍来自 members.ts
        member_cap = self._wait_out(member_out)
        self.assertIn("append_file=", member_cap, "member wrapper 未捕获 append 文件")
        member_content = member_cap.split("---append-content-begin---", 1)[-1] \
                                   .split("---append-content-end---", 1)[0].strip("\n")
        self.assertEqual(member_content, expected_member,
                         "混合团队 claude member system prompt 应来自 prompts/members.ts")


if __name__ == "__main__":
    unittest.main()
