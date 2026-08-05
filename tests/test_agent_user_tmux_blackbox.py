"""
Agent 用户接管 — 真实 tmux 黑盒回归测试
========================================

根因（用户确认 + 本文件黑盒复现）：
  Claude Code 的 env 生效优先级是「settings 文件 env > 普通进程 env」：
    1) 用户级 ~/.claude/settings.json 的 env 块会**覆盖**进程级 env 变量；
    2) 遗留的 ANTHROPIC_AUTH_TOKEN（非空）**优先于** ANTHROPIC_API_KEY；
    3) --model 是 CLI 参数，settings 的 env 覆盖不了它 → 因此只有 model 生效，
       base_url 与 key 未被接管（与本文件 claude-sim 探针观察一致）。

设计约束（leader 确认）：
  - 修复**不得**把 profile 凭据写进团队共享 .claude/settings.json
    （同队多成员/多 profile 会串号 + 敏感配置落盘风险）。
  - 采用每终端独立、优先级高于 user/project settings 的 Claude
    --settings <私有文件> 覆盖（私有临时/配置文件或等价机制）。
  - 选中 Claude profile 时同时处理 ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN /
    ANTHROPIC_BASE_URL / ANTHROPIC_MODEL，缺字段清除；
    none/disabled 语义不得破坏「使用系统默认」。

本文件为**独立黑盒**回归门禁：
  - 通过真实 tmux 入口（common.tmux_utils.tmux_spawn_member /
    mult_agent_mcp.launch_team_terminals）启动 leader 与 member 终端；
  - claude-sim 探针忠实模拟 Claude Code 的 settings/env 优先级，
    输出**有效** ANTHROPIC_* 值（指纹，绝不输出真实 key）；
  - 验证：三字段匹配所选 profile、切换不串号、并发隔离、
    私有 --settings 覆盖压过 user settings、AUTH_TOKEN 清除、none/disabled
    走系统默认。

未实现修复前（当前工作树），「user settings 覆盖 + AUTH_TOKEN 优先级」相关
用例将失败（证明 bug）；coder-claude 接入每终端 --settings 私有覆盖后应转绿。
探针只在临时目录写入假凭据指纹，绝不触碰真实 teams_data.json / 真实 tmux 会话。
"""

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from common import data_layer
from common.tmux_utils import tmux_spawn_member, tmux_session_name

import mult_agent_mcp  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 假凭据：仅测试用，绝不可能是真实 key
ALICE = ("sk-ant-fakealice", "https://alice.example/v1", "claude-alice-model")
BOB = ("sk-ant-fakebob", "https://bob.example/v1", "claude-bob-model")
CAROL = ("sk-ant-fakecarol", "https://carol.example/v1", "claude-carol-model")
# 敌意用户级 settings：错误 base_url + 遗留 AUTH_TOKEN + 错误 model
HOSTILE_USER_ENV = {
    "ANTHROPIC_BASE_URL": "https://user-settings.example/v1",
    "ANTHROPIC_AUTH_TOKEN": "stale-user-token",
    "ANTHROPIC_MODEL": "user-wrong-model",
}

_ENV_KEYS = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
)

SIM_WRAPPER_SRC = r'''#!/bin/bash
# claude-sim: 忠实模拟 Claude Code 的 settings env 优先级，供黑盒回归使用。
# 优先级: 进程 env  <  用户 settings env  <  项目 settings env  <  --settings env
# 认证: 非空 ANTHROPIC_AUTH_TOKEN 优先于 ANTHROPIC_API_KEY。
USER_SETTINGS="__USER_SETTINGS__"
OUT="__OUT__"
SETTINGS_FILE=""; model_arg=""; prev=""
for a in "$@"; do
  if [ "$prev" = "--model" ]; then model_arg="$a"; fi
  if [ "$prev" = "--settings" ]; then SETTINGS_FILE="$a"; fi
  prev="$a"
done
effective_api_key="${ANTHROPIC_API_KEY:-}"; effective_token="${ANTHROPIC_AUTH_TOKEN:-}"
effective_base_url="${ANTHROPIC_BASE_URL:-}"; effective_model="${ANTHROPIC_MODEL:-}"
apply_env_file() {
  local f="$1"; [ -f "$f" ] || return 0; local k v
  while IFS='=' read -r k v; do
    case "$k" in
      ANTHROPIC_BASE_URL) effective_base_url="$v";;
      ANTHROPIC_API_KEY) effective_api_key="$v";;
      ANTHROPIC_AUTH_TOKEN) effective_token="$v";;
      ANTHROPIC_MODEL) effective_model="$v";;
    esac
  done < <(/usr/bin/python3 -c "import json,sys;d=json.load(open(sys.argv[1]));[print(k+'='+v) for k,v in d.get('env',{}).items()]" "$f" 2>/dev/null)
}
apply_env_file "$USER_SETTINGS"
apply_env_file "$PWD/.claude/settings.json"
[ -n "$SETTINGS_FILE" ] && apply_env_file "$SETTINGS_FILE"
if [ -n "$effective_token" ]; then auth="token"; else auth="apikey"; fi
fp() { if [ -n "$1" ]; then printf '%s' "$1" | sha256sum | cut -c1-16; else echo none; fi }
{
  echo "effective_base_url=$effective_base_url"
  echo "effective_model=${model_arg:-$effective_model}"
  echo "effective_auth=$auth"
  echo "token_fp=$(fp "$effective_token")"
  echo "api_fp=$(fp "$effective_api_key")"
  echo "settings_file=${SETTINGS_FILE:-}"
} > "$OUT"
sleep 120
'''


def _fp(key: str) -> str:
    import hashlib
    return hashlib.sha256(key.encode()).hexdigest()[:16]


class AgentUserTmuxBlackBoxBase(unittest.TestCase):
    """公共 setUp/tearDown：隔离数据文件 + 环境 + tmux 会话清理。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.data_file = self.root / "teams_data.json"
        data_layer.set_data_file(self.data_file)
        self._sessions: list[str] = []
        # 实例/进程级唯一后缀：并发跑同一套件时不同实例互不干扰。
        # 固定 team/session 名会让两个并发 pytest 进程 kill 对方创建的 tmux
        # session（leader 用例曾 FAIL: send_keys can't find session）。
        self._uid = uuid.uuid4().hex[:8]
        self._env_backup = {k: os.environ.get(k) for k in _ENV_KEYS}
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        self.addCleanup(self._cleanup)

    def _team(self, base: str) -> str:
        """本测试实例唯一的 team 名（避免并发实例间 session/数据碰撞）。"""
        return f"{base}_{self._uid}"

    def _session(self, base: str) -> str:
        """本测试实例唯一的 tmux session 名。"""
        return f"mcp_{base}_{self._uid}"

    def _cleanup(self):
        # 只杀本实例创建（self._sessions 记录）的 session，绝不波及并发实例的终端
        for s in self._sessions:
            subprocess.run(["tmux", "kill-session", "-t", s],
                           capture_output=True, text=True)
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        data_layer._DATA_FILE_OVERRIDE = None
        try:
            self._tmp.cleanup()
        except Exception:  # noqa: BLE001
            pass

    # ---- helpers ----

    def _write_data(self, data: dict):
        from common.atomic_write import atomic_json_write
        atomic_json_write(self.data_file, data)

    def _hostile_user_settings(self) -> Path:
        p = self.root / "user_settings.json"
        p.write_text(json.dumps({"env": HOSTILE_USER_ENV}))
        return p

    def _make_sim(self, tag: str, user_settings: Path, out: Path) -> Path:
        p = self.root / f"claude-sim-{tag}.sh"
        p.write_text(
            SIM_WRAPPER_SRC
            .replace("__USER_SETTINGS__", str(user_settings))
            .replace("__OUT__", str(out))
        )
        p.chmod(0o755)
        return p

    def _read_out(self, p: Path, timeout: float = 8.0) -> dict:
        end = time.time() + timeout
        while time.time() < end and not p.exists():
            time.sleep(0.2)
        if not p.exists():
            return {}
        result = {}
        for line in p.read_text().splitlines():
            k, _, v = line.partition("=")
            if k:
                result[k] = v
        return result

    def _profile_team(self, user_settings: Path) -> dict:
        """含 alice/bob/carol 三个 claude profile 的团队数据。"""
        probeA = self._make_sim("A", user_settings, self.root / "outA.txt")
        probeB = self._make_sim("B", user_settings, self.root / "outB.txt")
        probeN = self._make_sim("N", user_settings, self.root / "outN.txt")
        probeD = self._make_sim("D", user_settings, self.root / "outD.txt")
        agent_users = {
            "alice": {"agent_type": "claude", "takeover_enabled": True,
                      "anthropic_api_key": ALICE[0], "anthropic_base_url": ALICE[1],
                      "anthropic_model": ALICE[2]},
            "bob": {"agent_type": "claude", "takeover_enabled": True,
                    "anthropic_api_key": BOB[0], "anthropic_base_url": BOB[1],
                    "anthropic_model": BOB[2]},
            "carol_disabled": {"agent_type": "claude", "takeover_enabled": False,
                               "anthropic_api_key": CAROL[0], "anthropic_base_url": CAROL[1],
                               "anthropic_model": CAROL[2]},
        }
        return {
            "agent_users": agent_users,
            "default_agent": "claude", "leader": "lead", "leader_type": "tmux",
            "workspace_dir": str(self.root / "ws"),
            "members": {
                "lead": {"agent": str(probeA), "role": "leader", "agent_user": "alice"},
                "memA": {"agent": str(probeA), "agent_user": "alice"},
                "memB": {"agent": str(probeB), "agent_user": "bob"},
                "memN": {"agent": str(probeN), "agent_user": "__none__"},
                "memD": {"agent": str(probeD), "agent_user": "carol_disabled"},
            },
        }

    def _spawn_member(self, team_name: str, member: str, session: str, new_session: bool):
        team_data = data_layer.load_data()["teams"][team_name]
        probe = Path(team_data["members"][member]["agent"])
        self._sessions.append(session)
        return tmux_spawn_member(session, member, str(probe), str(self.root / "ws"),
                                 new_session=new_session, team_name_for_permissions=team_name)


class UserSettingsEnvOverrideBlackBoxTests(AgentUserTmuxBlackBoxBase):
    """根因回归：用户级 settings env 覆盖进程 env → 只有 model 生效。

    修复前（当前工作树）失败：effective_base_url / effective_auth 被 user
    settings 覆盖，仅 effective_model（经 --model CLI 参数）正确。
    修复（每终端 --settings 私有覆盖）后应三字段均匹配 profile。
    """

    def _launch_alice(self) -> dict:
        team = self._team("bb_override")
        user_settings = self._hostile_user_settings()
        self._write_data({"teams": {team: self._profile_team(user_settings)}})
        rc, _, err = self._spawn_member(team, "memA", tmux_session_name(team), True)
        self.assertEqual(rc, 0, f"成员终端创建失败: {err}")
        out = self.root / "outA.txt"
        return self._read_out(out)

    def test_member_effective_env_matches_selected_profile_despite_user_settings(self):
        """三字段必须匹配所选 profile：修复前 FAIL（用户 settings 覆盖 base_url/key）。"""
        effective = self._launch_alice()
        self.assertIn("effective_base_url", effective)
        self.assertEqual(effective["effective_base_url"], ALICE[1],
                         "base_url 被 user settings 覆盖，未接管所选 profile")
        self.assertEqual(effective["effective_auth"], "token",
                         "凭据应经 AUTH_TOKEN 通道（中转站 Bearer 认证），而非仅 API_KEY")
        self.assertEqual(effective["api_fp"], _fp(ALICE[0]),
                         "API_KEY 未接管为所选 profile")
        self.assertEqual(effective["token_fp"], _fp(ALICE[0]),
                         "AUTH_TOKEN 未接管为所选 profile")
        self.assertEqual(effective["effective_model"], ALICE[2],
                         "model 应匹配所选 profile（经 --model）")

    def test_stale_auth_token_does_not_override_selected_key(self):
        """AUTH_TOKEN 通道：遗留 user token 必须被 profile key 接管（双通道同值）。"""
        effective = self._launch_alice()
        self.assertEqual(effective.get("effective_auth"), "token")
        self.assertEqual(effective.get("token_fp"), _fp(ALICE[0]),
                         "AUTH_TOKEN 必须承载 profile key，而非遗留 user token")
        self.assertNotEqual(effective.get("token_fp"), _fp("stale-user-token"),
                            "遗留 user token 不得残留")
        self.assertEqual(effective.get("api_fp"), _fp(ALICE[0]))

    def test_settings_override_mechanism_contract(self):
        """机制契约：--settings 私有文件优先级高于 user settings，双通道注入同一 key。

        独立于修复是否存在：验证 claude-sim 忠实实现「私有 --settings > user
        settings、AUTH_TOKEN 与 API_KEY 双通道同值」，从而证明该机制一旦由
        生产接入即可满足隔离要求。
        """
        team = self._team("bb_contract")
        user_settings = self._hostile_user_settings()
        self._write_data({"teams": {team: self._profile_team(user_settings)}})
        probe = self.root / "claude-sim-A.sh"

        priv = self.root / "alice_private_settings.json"
        priv.write_text(json.dumps({"env": {
            "ANTHROPIC_API_KEY": ALICE[0],
            "ANTHROPIC_AUTH_TOKEN": ALICE[0],  # 双通道同值：中转站走 Bearer
            "ANTHROPIC_BASE_URL": ALICE[1],
            "ANTHROPIC_MODEL": ALICE[2],
        }}))
        session = self._session("bb_contract")
        self._sessions.append(session)
        cmd = ["new-session", "-d", "-s", session, "-n", "memA", "-c", str(self.root / "ws"),
               "env", f"ANTHROPIC_API_KEY={ALICE[0]}", f"ANTHROPIC_BASE_URL={ALICE[1]}",
               f"ANTHROPIC_MODEL={ALICE[2]}",
               str(probe), "--settings", str(priv), "--model", ALICE[2]]
        r = subprocess.run(["tmux"] + cmd, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        effective = self._read_out(self.root / "outA.txt")
        self.assertEqual(effective.get("effective_base_url"), ALICE[1])
        self.assertEqual(effective.get("effective_auth"), "token")
        self.assertEqual(effective.get("api_fp"), _fp(ALICE[0]))
        self.assertEqual(effective.get("token_fp"), _fp(ALICE[0]))
        self.assertEqual(effective.get("effective_model"), ALICE[2])
        self.assertEqual(effective.get("settings_file"), str(priv))


class ProfileIsolationBlackBoxTests(AgentUserTmuxBlackBoxBase):
    """切换/并发隔离：不串号、none/disabled 走系统默认、不继承旧凭据。"""

    def test_profile_switch_no_cross_contamination(self):
        """同一成员 alice→bob：新环境必须匹配 bob，绝不残留 alice 凭据。"""
        team = self._team("bb_switch")
        user_settings = self._hostile_user_settings()
        self._write_data({"teams": {team: self._profile_team(user_settings)}})
        session = tmux_session_name(team)

        # 阶段 1: memA = alice
        rc, _, err = self._spawn_member(team, "memA", session, True)
        self.assertEqual(rc, 0, err)
        eff_a = self._read_out(self.root / "outA.txt")
        self.assertEqual(eff_a.get("effective_model"), ALICE[2])

        # 阶段 2: 切换 memA -> bob（重建终端，模拟重新接管）
        from common.atomic_write import atomic_json_write
        data = data_layer.load_data()
        data["teams"][team]["members"]["memA"]["agent_user"] = "bob"
        atomic_json_write(self.data_file, data)
        subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)
        self._sessions.remove(session)
        self._sessions.append(session)
        if (self.root / "outA.txt").exists():
            (self.root / "outA.txt").unlink()
        rc, _, err = self._spawn_member(team, "memA", session, True)
        self.assertEqual(rc, 0, err)
        eff_b = self._read_out(self.root / "outA.txt")
        # 修复后应断言；修复前 alice 断言即失败（证明 bug）
        self.assertEqual(eff_b.get("effective_base_url"), BOB[1])
        self.assertEqual(eff_b.get("api_fp"), _fp(BOB[0]))
        self.assertEqual(eff_b.get("token_fp"), _fp(BOB[0]))
        self.assertNotEqual(eff_b.get("api_fp"), _fp(ALICE[0]),
                            "切换后不得残留 alice 凭据")
        self.assertNotEqual(eff_b.get("token_fp"), _fp(ALICE[0]),
                            "切换后 AUTH_TOKEN 通道不得残留 alice 凭据")
        self.assertEqual(eff_b.get("effective_model"), BOB[2])

    def test_concurrent_profiles_isolated_no_cross_keys(self):
        """并发多 profile：memA(alice) 与 memB(bob) 各自生效，互不串号。"""
        team = self._team("bb_conc")
        user_settings = self._hostile_user_settings()
        self._write_data({"teams": {team: self._profile_team(user_settings)}})
        session = tmux_session_name(team)
        rc, _, err = self._spawn_member(team, "memA", session, True)
        self.assertEqual(rc, 0, err)
        rc, _, err = self._spawn_member(team, "memB", session, False)
        self.assertEqual(rc, 0, err)
        eff_a = self._read_out(self.root / "outA.txt")
        eff_b = self._read_out(self.root / "outB.txt")
        self.assertEqual(eff_a.get("effective_base_url"), ALICE[1])
        self.assertEqual(eff_b.get("effective_base_url"), BOB[1])
        self.assertEqual(eff_a.get("api_fp"), _fp(ALICE[0]))
        self.assertEqual(eff_a.get("token_fp"), _fp(ALICE[0]))
        self.assertEqual(eff_b.get("api_fp"), _fp(BOB[0]))
        self.assertEqual(eff_b.get("token_fp"), _fp(BOB[0]))
        self.assertNotEqual(eff_a.get("api_fp"), _fp(BOB[0]), "alice 不得读到 bob 的 key")
        self.assertNotEqual(eff_a.get("token_fp"), _fp(BOB[0]), "alice 的 AUTH_TOKEN 不得读到 bob 的 key")
        self.assertNotEqual(eff_b.get("api_fp"), _fp(ALICE[0]), "bob 不得读到 alice 的 key")
        self.assertNotEqual(eff_b.get("token_fp"), _fp(ALICE[0]), "bob 的 AUTH_TOKEN 不得读到 alice 的 key")
        self.assertEqual(eff_a.get("effective_model"), ALICE[2])
        self.assertEqual(eff_b.get("effective_model"), BOB[2])

    def test_none_uses_system_default_no_profile_injection(self):
        """agent_user=__none__：不注入任何 profile 凭据，走用户级系统默认。"""
        team = self._team("bb_none")
        user_settings = self._hostile_user_settings()
        self._write_data({"teams": {team: self._profile_team(user_settings)}})
        rc, _, err = self._spawn_member(team, "memN", tmux_session_name(team), True)
        self.assertEqual(rc, 0, err)
        eff = self._read_out(self.root / "outN.txt")
        # 系统默认 = 用户 settings 的 base_url / model；绝不带 profile key
        self.assertEqual(eff.get("effective_base_url"), HOSTILE_USER_ENV["ANTHROPIC_BASE_URL"])
        self.assertEqual(eff.get("effective_model"), HOSTILE_USER_ENV["ANTHROPIC_MODEL"])
        self.assertEqual(eff.get("api_fp"), "none", "__none__ 不得注入任何 profile key")
        self.assertNotIn(eff.get("api_fp"), {_fp(ALICE[0]), _fp(BOB[0])})

    def test_disabled_takeover_no_injection(self):
        """takeover_enabled=False：显式关闭接管 → 不注入 profile 凭据。"""
        team = self._team("bb_disable")
        user_settings = self._hostile_user_settings()
        self._write_data({"teams": {team: self._profile_team(user_settings)}})
        rc, _, err = self._spawn_member(team, "memD", tmux_session_name(team), True)
        self.assertEqual(rc, 0, err)
        eff = self._read_out(self.root / "outD.txt")
        self.assertEqual(eff.get("api_fp"), "none", "接管关闭时不得注入 carol 的 key")
        self.assertNotEqual(eff.get("effective_model"), CAROL[2],
                            "接管关闭时不得应用 carol 的 model")


class LeaderRealEntryBlackBoxTests(AgentUserTmuxBlackBoxBase):
    """leader 终端经真实 MCP 入口 launch_team_terminals 的 env 接管黑盒。"""

    def test_leader_effective_env_matches_profile_via_real_entry(self):
        team = self._team("bb_leader")
        user_settings = self._hostile_user_settings()
        self._write_data({"teams": {team: self._profile_team(user_settings)}})
        os.environ["MULT_AGENT_MCP_CONTEXT_DIR"] = str(self.root / "share")
        try:
            with mock.patch.object(mult_agent_mcp, "DATA_FILE", str(self.data_file)), \
                 mock.patch.object(mult_agent_mcp, "_ensure_codex_mcp", return_value="ok"):
                try:
                    result = mult_agent_mcp.launch_team_terminals(team, task="")
                finally:
                    mult_agent_mcp._stop_team_monitor(team)
                    mult_agent_mcp._kill_session(team)
        finally:
            os.environ.pop("MULT_AGENT_MCP_CONTEXT_DIR", None)
        self._sessions.append("mcp_" + team)
        self.assertNotIn("❌", result, result)
        eff = self._read_out(self.root / "outA.txt")
        self.assertEqual(eff.get("effective_base_url"), ALICE[1],
                         "leader base_url 未接管所选 profile（真实入口）")
        self.assertEqual(eff.get("api_fp"), _fp(ALICE[0]),
                         "leader key 未接管所选 profile（真实入口）")
        self.assertEqual(eff.get("token_fp"), _fp(ALICE[0]),
                         "leader AUTH_TOKEN 未接管所选 profile（真实入口）")
        self.assertEqual(eff.get("effective_model"), ALICE[2])


class SecurityAndIsolationContractTests(AgentUserTmuxBlackBoxBase):
    """reviewer 补充核对项：跨团队同名碰撞、0700/0600 权限、仅 ANTHROPIC_*、敏感值不入命令行。"""

    def _launch_and_out(self, team_name: str, member: str, session: str, out: Path) -> dict:
        team_data = data_layer.load_data()["teams"][team_name]
        probe = Path(team_data["members"][member]["agent"])
        self._sessions.append(session)
        rc, _, err = tmux_spawn_member(session, member, str(probe), str(self.root / "ws"),
                                       new_session=True, team_name_for_permissions=team_name)
        self.assertEqual(rc, 0, f"成员终端创建失败: {err}")
        return self._read_out(out)

    def test_cross_team_same_name_settings_not_collide(self):
        """跨团队同名成员 + 同名异配置 profile 不得共享 settings 文件、不得串号。"""
        user_settings = self._hostile_user_settings()
        probe_c1 = self._make_sim("c1", user_settings, self.root / "out_c1.txt")
        probe_c2 = self._make_sim("c2", user_settings, self.root / "out_c2.txt")
        from common.atomic_write import atomic_json_write
        data = {"teams": {
            self._team("teamX"): {"agent_users": {
                "alice": {"agent_type": "claude", "takeover_enabled": True,
                          "anthropic_api_key": ALICE[0], "anthropic_base_url": ALICE[1],
                          "anthropic_model": ALICE[2]}},
                "default_agent": "claude", "leader": "lead",
                "members": {"dev": {"agent": str(probe_c1), "agent_user": "alice"}}},
            self._team("teamY"): {"agent_users": {
                "bob": {"agent_type": "claude", "takeover_enabled": True,
                        "anthropic_api_key": BOB[0], "anthropic_base_url": BOB[1],
                        "anthropic_model": BOB[2]}},
                "default_agent": "claude", "leader": "lead",
                "members": {"dev": {"agent": str(probe_c2), "agent_user": "bob"}}},
        }}
        atomic_json_write(self.data_file, data)

        teamX, teamY = self._team("teamX"), self._team("teamY")
        eff1 = self._launch_and_out(teamX, "dev", self._session("teamX"), self.root / "out_c1.txt")
        eff2 = self._launch_and_out(teamY, "dev", self._session("teamY"), self.root / "out_c2.txt")

        p1 = eff1.get("settings_file", "")
        p2 = eff2.get("settings_file", "")
        self.assertTrue(p1 and p2, f"应生成私有 settings 文件: {p1!r} / {p2!r}")
        self.assertNotEqual(p1, p2, "跨团队同名成员 settings 文件必须不同（不得碰撞）")
        self.assertIn("teamX", p1)
        self.assertIn("teamY", p2)
        self.assertEqual(eff1.get("effective_base_url"), ALICE[1], "teamX/dev 应为 alice")
        self.assertEqual(eff2.get("effective_base_url"), BOB[1], "teamY/dev 应为 bob")
        self.assertEqual(eff1.get("api_fp"), _fp(ALICE[0]))
        self.assertEqual(eff1.get("token_fp"), _fp(ALICE[0]))
        self.assertEqual(eff2.get("api_fp"), _fp(BOB[0]))
        self.assertEqual(eff2.get("token_fp"), _fp(BOB[0]))

    def test_private_settings_file_permissions_0700_dir_0600_file(self):
        """私有 settings：目录 0700、文件 0600，仅本人可读。"""
        team = self._team("bb_perm")
        user_settings = self._hostile_user_settings()
        self._write_data({"teams": {team: self._profile_team(user_settings)}})
        eff = self._launch_and_out(team, "memA", self._session("bb_perm"), self.root / "outA.txt")
        path = Path(eff.get("settings_file", ""))
        self.assertTrue(path.exists(), f"私有 settings 文件应存在: {path}")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600,
                         f"settings 文件权限应为 0600（实际 {oct(path.stat().st_mode & 0o777)}）")
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700,
                         f"settings 目录权限应为 0700（实际 {oct(path.parent.stat().st_mode & 0o777)}）")

    def test_settings_file_only_anthropic_vars_no_openai(self):
        """settings env 只处理影响 Claude provider 的 ANTHROPIC_*，不得清空 OPENAI_*。"""
        team = self._team("bb_env")
        user_settings = self._hostile_user_settings()
        self._write_data({"teams": {team: self._profile_team(user_settings)}})
        eff = self._launch_and_out(team, "memA", self._session("bb_env"), self.root / "outA.txt")
        path = Path(eff.get("settings_file", ""))
        self.assertTrue(path.exists())
        env = json.loads(path.read_text())["env"]
        self.assertTrue(env, "settings env 不应为空")
        for k in env:
            self.assertTrue(k.startswith("ANTHROPIC_"), f"settings 只应含 ANTHROPIC_*，实际有 {k}")
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("OPENAI_BASE_URL", env)
        self.assertNotIn("CODEX_MODEL", env)
        self.assertEqual(env["ANTHROPIC_API_KEY"], ALICE[0], "API_KEY 应写入 settings 文件")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], ALICE[0],
                         "AUTH_TOKEN 双通道注入同一 key（中转站 Bearer 认证）")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], ALICE[1])

    def test_sensitive_key_not_on_command_line(self):
        """敏感 key 不得出现在终端进程命令行（经 /proc cmdline 实测）。

        修复前（env 前缀仍注入 ANTHROPIC_API_KEY）→ FAIL；修复后 key 只进
        私有 settings 文件，命令行不得含原始 key。
        """
        team = self._team("bb_nocmd")
        user_settings = self._hostile_user_settings()
        self._write_data({"teams": {team: self._profile_team(user_settings)}})
        session = self._session("bb_nocmd")
        eff = self._launch_and_out(team, "memA", session, self.root / "outA.txt")
        self.assertTrue(eff.get("settings_file"), "应生成私有 settings 文件")
        pane_pid = subprocess.run(
            ["tmux", "display-message", "-t", f"{session}:memA", "-p", "#{pane_pid}"],
            capture_output=True, text=True,
        ).stdout.strip()
        self.assertTrue(pane_pid.isdigit(), f"无法获取 pane pid: {pane_pid}")
        try:
            raw = Path(f"/proc/{pane_pid}/cmdline").read_bytes()
        except OSError:
            self.fail(f"无法读取 /proc/{pane_pid}/cmdline")
        cmdline = " ".join(p.decode(errors="replace") for p in raw.split(b"\0") if p)
        self.assertNotIn(ALICE[0], cmdline,
                         "敏感 key 不得出现在命令行（应仅存在于私有 settings 文件）")

    def test_settings_dir_chmod_failure_fails_closed(self):
        """凭据目录权限无法收紧到 0700 时必须 fail closed：不得写入任何 secret 文件。

        当前实现 _ensure_settings_dir 的 chmod(0700) 失败被静默吞掉后继续写文件
        → 本测试失败（证明需修正）；修复后应在 chmod 失败时中止，不落盘。
        """
        import common.tmux_utils as tu
        team = self._team("bb_fc")
        user_settings = self._hostile_user_settings()
        self._write_data({"teams": {team: self._profile_team(user_settings)}})

        real_chmod = os.chmod

        def fake_chmod(path, mode, *a, **k):
            if mode == 0o700:  # 仅拦截目录 0700 收紧失败
                raise OSError("permission denied tightening dir")
            return real_chmod(path, mode, *a, **k)

        with mock.patch("common.tmux_utils.os.chmod", side_effect=fake_chmod):
            try:
                tu.build_agent_user_claude_settings(team, "memA")
            except (OSError, RuntimeError):
                pass  # fail-closed 允许以异常中止（OSError / RuntimeError）
        base = data_layer.get_data_file().parent / ".agent_user_settings"
        files = list(base.glob("*.json")) if base.exists() else []
        self.assertEqual(files, [],
                         f"chmod(0700) 失败后不得写入 secret 文件，实际存在: {files}")

    def test_profile_delete_cleans_private_settings_residue(self):
        """删除 profile 后，私有 settings 中的旧凭据不得无限残留。

        当前 agent_user_delete_sweep 不清理 .agent_user_settings 磁盘文件
        → 本测试失败（证明残留）；修复后删除 profile 应同步清理其 settings 文件
        （或提供明确的保留/清理策略与说明）。
        """
        import common.tmux_utils as tu
        from common.atomic_write import atomic_json_write
        team = self._team("bb_del")
        user_settings = self._hostile_user_settings()
        self._write_data({"teams": {team: self._profile_team(user_settings)}})
        eff = self._launch_and_out(team, "memA", self._session("bb_del"), self.root / "outA.txt")
        path = Path(eff.get("settings_file", ""))
        self.assertTrue(path.exists(), "应先生成私有 settings 文件")
        data = data_layer.load_data()
        tu.agent_user_delete_sweep(data, "alice")
        atomic_json_write(self.data_file, data)
        self.assertFalse(path.exists(),
                         "删除 profile 后旧凭据 settings 文件应被清理（或提供清理策略）")

    def test_profile_rename_cleans_private_settings_residue(self):
        """重命名 profile 后，旧 key 的私有 settings 残留必须一并清理。

        重命名让旧 key 不再存在，其旧凭据 settings 文件若残留则随旧 key 无限留活。
        agent_user_rename_sweep 应同步清理旧 key 的 .agent_user_settings 文件
        （或提供明确的保留/清理策略与说明）。
        """
        import common.tmux_utils as tu
        from common.atomic_write import atomic_json_write
        team = self._team("bb_ren")
        user_settings = self._hostile_user_settings()
        self._write_data({"teams": {team: self._profile_team(user_settings)}})
        eff = self._launch_and_out(team, "memA", self._session("bb_ren"), self.root / "outA.txt")
        path = Path(eff.get("settings_file", ""))
        self.assertTrue(path.exists(), "应先生成私有 settings 文件")
        data = data_layer.load_data()
        tu.agent_user_rename_sweep(data, "alice", "alice__v2")
        atomic_json_write(self.data_file, data)
        self.assertFalse(path.exists(),
                         "重命名 profile 后旧 key 的 settings 残留应被清理")

    def test_purge_filename_hash_matching_is_precise(self):
        """门禁: purge 按文件名末尾 hashed profile 分量精确匹配，不误删跨团队/跨成员。

        settings JSON 已不含 _agent_user_key（Claude 官方 env-only），归属只能靠
        文件名 profile 分量表达。purge 必须:
          - 只删 profile 分量匹配的文件；
          - 跨团队同名成员 / 跨成员同 profile 的文件不得被误删；
          - 未指定 team/member 时仍按 profile 分量精确过滤（不宽匹配）。
        """
        import common.tmux_utils as tu
        from common.atomic_write import atomic_json_write
        # 用生产路径函数构造文件名（6 分量: team__h__member__h__profile__h），保证与真实产物一致
        d = data_layer.get_data_file().parent / ".agent_user_settings"
        d.mkdir(parents=True, exist_ok=True)
        files = {
            "target_a": tu._agent_user_settings_path("teamA", "mem1", "alice"),
            "target_b": tu._agent_user_settings_path("teamB", "mem2", "alice"),
            "other_bob": tu._agent_user_settings_path("teamA", "mem1", "bob"),
            # 任意命名（无 profile 分量归属）：不得误删
            "random": d / "legacy_arbitrary_name.json",
        }
        for f in files.values():
            atomic_json_write(f, {"env": {"ANTHROPIC_BASE_URL": "https://claude.internal"}})
        removed, failed = tu.purge_agent_user_settings("alice")
        self.assertEqual(failed, [], f"不应有删除失败: {failed}")
        self.assertEqual(removed, 2, f"应精确删除 2 个 alice 文件，实际 {removed}")
        self.assertFalse(files["target_a"].exists(), "target_a 应被删除")
        self.assertFalse(files["target_b"].exists(), "target_b 应被删除")
        self.assertTrue(files["other_bob"].exists(), "bob profile 文件不得被误删")
        self.assertTrue(files["random"].exists(), "任意命名文件不得被误删")

    def test_settings_json_root_keys_only_env(self):
        """门禁: 生成给 Claude 的 --settings JSON 根键必须只含 Claude 官方字段 env。

        自定义根字段（如 _agent_user_key）可能使 Claude Code 的 settings schema
        校验拒绝整文件（随版本可能从"容忍"变"拒绝"），导致真实终端仍只 model 生效
        —— 这正是"表面测试绿但真实终端失效"的风险点。本门禁锁定契约：
        根键集合 == {env}，不得有任何非 Claude 官方字段。
        """
        team = self._team("bb_root")
        user_settings = self._hostile_user_settings()
        self._write_data({"teams": {team: self._profile_team(user_settings)}})
        eff = self._launch_and_out(team, "memA", self._session("bb_root"), self.root / "outA.txt")
        path = Path(eff.get("settings_file", ""))
        self.assertTrue(path.exists(), "应生成私有 settings 文件")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(set(data.keys()), {"env"},
                         f"settings 根键必须只含 env（Claude 官方字段），实际: {sorted(data.keys())}")

    def test_real_claude_doctor_accepts_and_loads_settings_env(self):
        """门禁: 真实 claude 探针 — 生成文件必须被本机 claude 加载且 env 生效。

        用真实 claude doctor（纯离线、读 settings、不触发 API、不泄 key）验证：
          - 文件被接受：doctor 不报 schema/invalid 错误，退出码 0；
          - env 生效：在干净环境（env -i）下 doctor 输出能体现 --settings 文件
            env 块的 ANTHROPIC_BASE_URL（"custom endpoint" 提示），证明文件
            未被忽略——避免 claude-sim 忽略未知字段造成"假绿"。
        本机无 claude CLI 时跳过（CI 无 claude 也可跑其余门禁）。
        """
        claude = shutil.which("claude")
        if not claude:
            self.skipTest("本机无 claude CLI，跳过真实探针门禁")
        team = self._team("bb_claude")
        user_settings = self._hostile_user_settings()
        self._write_data({"teams": {team: self._profile_team(user_settings)}})
        eff = self._launch_and_out(team, "memA", self._session("bb_claude"), self.root / "outA.txt")
        path = Path(eff.get("settings_file", ""))
        self.assertTrue(path.exists(), "应生成私有 settings 文件")
        # 干净环境：只保留 HOME/PATH（claude doctor 需要），清掉一切继承的 ANTHROPIC_*
        env = {"HOME": os.environ.get("HOME", ""), "PATH": os.environ.get("PATH", "")}
        probe_dir = self.root / "probe_work"
        probe_dir.mkdir(exist_ok=True)
        # 复制 settings 为 .claude/settings.json（doctor 从 cwd 读项目 settings）
        (probe_dir / ".claude").mkdir(exist_ok=True)
        import shutil as _sh
        _sh.copyfile(path, probe_dir / ".claude" / "settings.json")
        r = subprocess.run(
            [claude, "doctor"],
            cwd=str(probe_dir), env=env, capture_output=True, text=True, timeout=60,
        )
        combined = (r.stdout or "") + "\n" + (r.stderr or "")
        self.assertEqual(r.returncode, 0,
                         f"claude doctor 应接受该 settings 文件（rc={r.returncode}）: {combined[:400]}")
        # env 生效证据：base_url 来自 settings env，被 doctor 报告为 custom endpoint
        self.assertIn("ANTHROPIC_BASE_URL", combined,
                      "claude doctor 应读到 settings env 的 base_url（文件未被忽略）")
        self.assertNotIn("_agent_user_key", combined)  # 未知字段不应被 claude 使用/提及


if __name__ == "__main__":
    unittest.main()
