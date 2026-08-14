"""prompt_template —— prompts/*.ts 纯 Python 解析器 + 通道渲染（无 Node/TS runtime）。

设计基线: docs/prompt_template_runtime_design.md + docs/system_prompt_injection_audit.md。

约定（@channel 是 system 判定的**唯一权威**）:
  - 每通道一个 ``export function <name>(vars: XxxVars): string { return `...`; }``；
  - 函数上方 JSDoc 的 ``@channel system|initial|recovery|task|wakeup`` 标注决定通道；
    缺失默认 ``user``（fail-safe，绝不默认 system）；
  - 模板体只允许 ``${v.field}`` 简单占位；其他 ``${...}``（表达式/嵌套/非 v.field）→
    解析错误（无 Node 求值）；
  - ``system`` 通道函数禁动态字段（task/recoverySection/teammates，见 DYNAMIC_FIELDS），
    动态段由调用方经 initial/recovery/task 通道注入——避免动态内容被冻结进 system 文件
    （修复原 claude_identity_file(leader=True) 渲染 _leader_system_prompt 冻结 recovery 的缺陷）。

定位: 相对模块 ``__file__``（PROJECT_DIR/prompts/），不依赖 cwd；支持
``MULTI_AGENT_MCP_PROMPTS_DIR`` 环境变量覆盖（打包独立布放/测试注入坏模板）。
缓存: mtime 键控解析缓存（只缓存 parse 结果，不缓存渲染；业务改 .ts → 下次渲染生效，
      新会话生效，不跨会话热替换）。
失败: 渲染入口 ``render_template`` 抛 ``PromptTemplateError``（携带文件+原因）；由调用方
      (prompt_registry) 决定回退（last-good → 内建最小身份），spawn 路径永不因此崩溃。
"""

import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

# system 通道禁用的动态字段（静态 system 文件不得冻结动态内容，C4）。
DYNAMIC_FIELDS = ("task", "recoverySection", "teammates")

# 合法 @channel 值。
CHANNELS = ("system", "initial", "recovery", "task", "wakeup")

# 最近一次解析错误（可观测/测试）。
last_error: str | None = None
_last_error_lock = threading.Lock()


class PromptTemplateError(Exception):
    """prompts/*.ts 模板定位/解析/渲染错误（携带文件+原因，供调用方安全回退）。"""


@dataclass
class TemplateFunction:
    """单个通道函数：名称 + @channel 通道 + 反引号模板体。"""

    name: str
    channel: str  # system / initial / recovery / task / wakeup / user
    body: str     # 反引号内模板体（已 strip 首尾空白，未做占位符渲染）


@dataclass
class ParsedTemplate:
    """解析结果：按函数名索引的通道函数集合。"""

    path: str
    functions: dict[str, TemplateFunction]


def _prompts_dir() -> Path:
    env = os.environ.get("MULTI_AGENT_MCP_PROMPTS_DIR", "").strip()
    if env:
        return Path(env)
    # 相对模块 __file__：repo 布局 = 项目根/prompts；打包安装（prompts/ 随包）同样可寻。
    return Path(__file__).resolve().parent.parent / "prompts"


def template_path(name: str, prompts_dir: Path | str | None = None) -> Path:
    """定位 ``prompts/{name}.ts`` 绝对路径（禁 cwd 相对路径，A3/D2）。

    ``prompts_dir`` 可选覆盖：registry 注入其可 patch 的目录解析、测试注入临时模板；
    默认经 ``_prompts_dir()``（模块相对 __file__ + MULTI_AGENT_MCP_PROMPTS_DIR 逃生阀）。
    """
    base = Path(prompts_dir) if prompts_dir is not None else _prompts_dir()
    path = (base / f"{name}.ts").resolve()
    if not path.is_file():
        raise PromptTemplateError(f"prompts/{name}.ts 不存在: {path}")
    return path


# ---------------------------------------------------------------------------
# 解析（纯文本，不执行 TypeScript）
# ---------------------------------------------------------------------------

_FUNC_RE = re.compile(r"export\s+function\s+(\w+)\s*\([^)]*\)\s*:\s*string\s*\{")
_RETURN_BACKTICK_RE = re.compile(r"return\s*`")


def _extract_jsdoc(pre: str) -> str | None:
    """提取紧邻函数前的 JSDoc 注释块（只允许空白间隔；无则 None）。

    用 rfind 定位**最末** ``*/`` 与其 ``/**`` 起点，而非正则 ``.*?``——避免文件头/
    接口字段注释被非贪婪匹配误吞（``@channel`` 误读成头注释散文，B5）。
    """
    close = pre.rfind("*/")
    if close == -1:
        return None
    if pre[close + 2:].strip():
        return None  # */ 与函数之间有非空白内容 → 不归属该函数
    open_ = pre.rfind("/**", 0, close)
    if open_ == -1:
        return None
    return pre[open_:close + 2]


def _channel_from_jsdoc(jsdoc: str | None) -> str:
    m = re.search(r"@channel\s+(\w+)", jsdoc or "")
    if not m:
        return "user"  # 缺失 @channel 默认 user（fail-safe，绝不默认 system）
    ch = m.group(1)
    if ch not in CHANNELS:
        raise PromptTemplateError(f"非法 @channel: {ch!r}（合法值 {CHANNELS}）")
    return ch


def _extract_body(text: str, fn_start: int, path: str, name: str) -> str:
    """提取 ``return `...` `` 模板体（扫描到未转义闭合反引号；处理 \\` 与 \\${ 转义）。

    闭合判定加固：找到第一个未转义反引号后，其后的非空白字符必须是语句结束符
    ``;``/``}``，否则说明该反引号是模板体内部的**未转义**反引号（典型：作者想给
    `` `word` `` 加反引号高亮却忘了转义），立即报错而非静默截断——旧实现会在该处
    截断模板体，静默吞掉后续动态字段/指令且不触发回退（运行时静默损坏）。
    """
    ret = _RETURN_BACKTICK_RE.search(text, fn_start)
    if not ret:
        raise PromptTemplateError(f"{path}: 函数 {name} 缺 return `...` 模板体")
    start = ret.end()
    i, n = start, len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2  # 转义序列（\\`、\\${、\\\\ 等）整体跳过
            continue
        if c == "`":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j >= n or text[j] not in ";}":
                raise PromptTemplateError(
                    f"{path}: 函数 {name} 模板体第 {i + 1} 字符处发现未转义反引号"
                    "（模板体内反引号需转义为 \\`；闭合模板体的反引号后应为 ; 或 }）")
            body = text[start:i].replace("\r\n", "\n").replace("\r", "\n")
            return body.strip()
        i += 1
    raise PromptTemplateError(f"{path}: 函数 {name} 模板体未闭合（缺闭合反引号）")


def _parse(text: str, path: str) -> ParsedTemplate:
    funcs: dict[str, TemplateFunction] = {}
    for m in _FUNC_RE.finditer(text):
        name = m.group(1)
        # 函数前紧邻的 JSDoc（只允许空白间隔）决定 @channel；无标注默认 user
        jsdoc = _extract_jsdoc(text[: m.start()])
        channel = _channel_from_jsdoc(jsdoc)
        body = _extract_body(text, m.start(), path, name)
        if name in funcs:
            raise PromptTemplateError(f"{path}: 重复的通道函数 {name}")
        funcs[name] = TemplateFunction(name=name, channel=channel, body=body)
    if not funcs:
        raise PromptTemplateError(f"{path}: 未找到任何 export function ...: string {{ return `...` }} 通道函数")
    return ParsedTemplate(path=path, functions=funcs)


# ---------------------------------------------------------------------------
# 缓存（F1/F3：只缓存 parse 结果；mtime 变化即重解析，渲染结果不缓存）
# ---------------------------------------------------------------------------

_parse_cache: dict[tuple[str, int], ParsedTemplate] = {}
_parse_cache_lock = threading.Lock()


def load_parsed(name: str, prompts_dir: Path | str | None = None) -> ParsedTemplate:
    path = template_path(name, prompts_dir)
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError as e:
        raise PromptTemplateError(f"prompts/{name}.ts 无法访问: {e}") from e
    key = (str(path), mtime)
    with _parse_cache_lock:
        parsed = _parse_cache.get(key)
    if parsed is not None:
        return parsed
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise PromptTemplateError(f"prompts/{name}.ts 读取失败: {e}") from e
    parsed = _parse(text, str(path))
    with _parse_cache_lock:
        _parse_cache[key] = parsed
    return parsed


# ---------------------------------------------------------------------------
# 渲染（单遍插值，A1：值原样写入，不做二次展开/执行）
# ---------------------------------------------------------------------------

def render_body(body: str, vars: dict[str, str]) -> str:
    """单遍渲染模板体。

    - ``${v.field}``：替换为 vars[field]（值含 ``${...}``/``$(...)``/反引号也原样保留，不二次展开）；
    - ``\\`` → ``\\``、``\\` `` → `` ` ``、``\\${`` → ``${``（字面，不替换）；
    - 非法占位符（表达式/嵌套/非 ``v.field``）或未知字段 → PromptTemplateError（B4）。
    """
    out: list[str] = []
    i, n = 0, len(body)
    while i < n:
        c = body[i]
        if c == "\\":
            nxt = body[i + 1] if i + 1 < n else ""
            if nxt == "`" or nxt == "\\":
                out.append(nxt)
                i += 2
                continue
            if nxt == "$" and i + 2 < n and body[i + 2] == "{":
                out.append("${")  # 字面 ${，不替换
                i += 3
                continue
            out.append("\\")
            i += 1
            continue
        if c == "$" and i + 1 < n and body[i + 1] == "{":
            j = body.find("}", i + 2)
            if j == -1:
                raise PromptTemplateError("模板体存在未闭合的 ${...}")
            expr = body[i + 2:j].strip()
            fm = re.fullmatch(r"v\.([A-Za-z_][A-Za-z0-9_]*)", expr)
            if not fm:
                raise PromptTemplateError("非法占位符 ${" + expr + "}（仅支持 ${v.field} 简单字段）")
            field = fm.group(1)
            if field not in vars:
                raise PromptTemplateError("未知占位符 ${v." + field + "}（vars 未提供）")
            out.append(vars[field])  # 单遍：值原样写入，不二次解析
            i = j + 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def render_template(
    name: str, fn_name: str, vars: dict[str, str], prompts_dir: Path | str | None = None,
) -> str:
    """定位 + 解析 + 单遍渲染指定通道函数模板体。

    任何失败（缺失/语法错/占位符越界/system 禁动态字段）抛 PromptTemplateError，
    并记录到 ``last_error`` 供观测。调用方（prompt_registry）据此安全回退，
    spawn 路径永不因此崩溃。
    """
    global last_error
    try:
        parsed = load_parsed(name, prompts_dir)
        fn = parsed.functions.get(fn_name)
        if fn is None:
            raise PromptTemplateError(f"{parsed.path}: 未找到通道函数 {fn_name}")
        if fn.channel == "system":
            for df in DYNAMIC_FIELDS:
                pat = "${v." + df + "}"
                if pat in fn.body:
                    raise PromptTemplateError(
                        f"{parsed.path}: system 通道函数 {fn_name} 禁动态字段 {pat}（C4）")
        out = render_body(fn.body, vars)
        with _last_error_lock:
            last_error = None
        return out
    except PromptTemplateError as e:
        with _last_error_lock:
            last_error = str(e)
        raise
    except Exception as e:  # 兜底：任何意外转成结构化错误，不静默
        msg = f"{name}.ts 渲染异常: {e}"
        with _last_error_lock:
            last_error = msg
        raise PromptTemplateError(msg) from e
