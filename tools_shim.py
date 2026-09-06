# -*- coding: utf-8 -*-
"""
tools_shim.py — webTools 提示词模拟(为 Gemini 网页版提供 OpenAI 兼容的工具调用)

原理:gemini_webapi 不支持原生函数调用,这里用"提示词协议 + 标签包裹 + 校验打回"模拟:
  1. 把 OpenAI 的 tools 注入 system 协议,要求模型用 <tool_call>...</tool_call> 标签包裹 JSON 输出
  2. 服务端剥标签、容错解析、校验(工具名+arguments 为对象)
  3. 校验失败/破损/协议回声 → 携带具体错误打回重试(≤2 次)
  4. 仍失败 → 剥离所有标签与协议回声后降级为纯文本返回(绝不外泄控制文本)
"""
from __future__ import annotations

import json
import re
import uuid

# 支持未闭合截断:匹配到 </tool_call> 或文本末尾(模型输出中断时挽救)
TOOL_CALL_RE = re.compile(r"<tool_call>\s*(.*?)\s*(?:</tool_call>|$)", re.DOTALL)
# 代码块遮蔽:成对围栏或行内代码(用于"正文讨论协议"免检)
CODE_FENCE_RE = re.compile(r"```[\s\S]*?```|`[^`\n]*`")
# Markdown 链接污染还原: [url](url) → url
MD_LINK_RE = re.compile(r"\[(https?://[^\]\s]+)\]\((?:https?://[^\)\s]+)\)")
# 协议回声剥除: [工具调用协议] 标题起,至"唯一。"或文本末尾
PROTOCOL_ECHO_RE = re.compile(r"\[工具调用协议\][\s\S]*?(?:注意[::].*?唯一。|\Z)")
# 破损特征(结构指纹): <tool_call> 后紧跟 `{`(真调用破损/截断) —— 正文自然讨论后接文字,不误报
BROKEN_TAG_JSON_RE = re.compile(r"<tool_call>\s*\{")
# 空开标签直接结尾(截断到只剩标签) —— 也算真截断
BROKEN_TAG_EOF_RE = re.compile(r"<tool_call>\s*$")
# 伪造工具结果回显:模型把历史里的工具结果口吻搬进自己的正文。
# 兼容中文/英文常见变体,允许前面误带 Human:/Assistant: 前缀(角色错乱的另一种表现)。
FAKE_RESULT_RE = re.compile(
    r"(?m)^[ \t]*(?:(?:Human|System|User|Assistant)\s*[:：]\s*)?"
    r"(?:工具调用结果|工具执行结果|工具返回结果|工具运行结果|工具输出|工具结果"
    r"|Tool\s*(?:call\s*)?result|Tool\s*output|Tool\s*returned|Tool\s*response)"
    r"\s*[:：]"
)
# 角色错乱:assistant 输出以 Human:/System:/User: 行开头(把自己当成别的说话人)
ROLE_CONFUSION_RE = re.compile(r"(?m)^[ \t]*(?:Human|System|User)\s*[:：]")
# 计划式收尾:模型只输出"计划/方案"文本就结束回合(未实际执行)。
# 在执行中的多步任务里这是"假完成"—— Agent 客户端会因此停在计划上等人工。
# v2: 扩充中英文变体;配合 PLAN_LIST_RE 结构指纹抓"裸编号清单"计划(无引导词直接列步骤)。
PLAN_ONLY_RE = re.compile(
    r"(?im)^[ \t]*(?:我的计划是|我计划|我制定了?计划|制定计划|执行计划|任务计划如下|计划如下|方案如下|"
    r"我的方案是|执行方案|接下来我会?|接下来我将|我会先|我将先|我先.{0,10}(?:然后|再|接着)|首先我|第一步是|"
    r"我的步骤|执行步骤|步骤如下|流程如下|行动方案|行动计划|分(?:几|\d+)步|待办事项|待办清单|"
    r"我打算|我准备|以下是.{0,8}(?:计划|方案|步骤)|计划[:：]|步骤[:：]|方案[:：]|思路[:：]|"
    r"here'?s (?:my|the) plan|my plan(?: is)?[:]?|plan[:：]|next steps?|action plan|"
    r"i'?ll (?:first|start|now|begin)|i will (?:first|start|now|begin)|let me (?:first|start|begin)|"
    r"i(?:'m| am) going to (?:first|start|begin)|step \d[.:：]|to[- ]do list|todo[:：])"
)
# 裸清单计划结构指纹: ≥2 行编号/勾选任务列表(无引导词也命中)
PLAN_LIST_RE = re.compile(r"^[ \t]*(?:\d{1,2}[.、)]\s+\S|[-*•]\s+\[[ x]?\]?\s*\S|[-*•]\s+\S)")
# 过去式完成标记: 出现即视为"已完成总结"而非计划(修复了/已创建/done/completed...)
PLAN_PAST_RE = re.compile(
    r"已[^,，。\n\s]{0,8}?(?:完成|修复|创建|删除|读取|写入|执行|运行|成功|处理|生成|安装|编译|解决|提交|验证|通过|更新|部署|发布|实现)"
    r"|(?:完成|成功)了|已经好了|全部做完|(?:was|were|has been|have been)\s+\w+ed\b|\b(?:done|completed|finished|fixed|created|successfully|succeeded)\b",
    re.IGNORECASE,
)
# 清单行级过去式: 单行含"了/过/已/成功"等 → 该行是已完成项而非待办项
_PLAN_LINE_PAST_RE = re.compile(r"了|过|已|成功|done|completed|finished|fixed|updated|created|successfully", re.IGNORECASE)
# 用户消息本身在要"计划/方案" → 第一轮计划收尾属正常回答,不拦截(防误伤"帮我写个计划")。
# F4 收紧: 必须是"请求动词+计划名词"邻近共现(或名词作疑问主体)才算要计划;
# 旧版裸匹配"步骤/思路"等名词会把"把部署步骤都执行一遍"这类执行型指令误判为要计划,放走假执行。
PLAN_ASK_RE = re.compile(
    r"(?:制定|写出?|给出?|提供|设计|规划|帮我?(?:做|写|出|列|设计)?|告诉我)[^,。!?\n]{0,8}(?:一份|个|套)?(?:详细的?|完整(的)?|执行|行动)?(?:计划|方案|思路|大纲|roadmap|outline)"
    r"|(?:列出?|罗列|列一下)[^,。!?\n]{0,8}(?:步骤|清单|计划|方案)"
    r"|(?:计划|方案|思路|大纲)(?:是什么|是啥|怎么样|咋样|如何)"
    r"|(?:什么|啥)(?:计划|方案|思路)"
    r"|(?:what|which).{0,16}\b(?:plan|approach|roadmap)\b"
    r"|(?:write|give|make|create|draft|provide)\s+(?:me\s+)?(?:a\s+|an\s+|the\s+)?(?:\w+\s+){0,2}(?:plan|outline|roadmap|approach)\b"
    r"|\bhow\s+(?:do|to|should|would)\b.{0,24}\b(?:plan|approach|proceed)\b",
    re.IGNORECASE,
)
# 谎报完成: 声称任务已完成(供 GEMINI_COMPLETION_VERIFY=on 时的自查回合;完成总结带过去式证据的不算)
COMPLETION_CLAIM_RE = re.compile(
    r"(?:任务|所有|全部|都)?(?:已经)?(?:全部)?完成(?:了|啦)?|搞定了?|已解决|任务结束|all (?:done|completed)|task (?:is )?complete",
    re.IGNORECASE,
)
# 叙述式执行动词: 与【声明的工具名】共现即判"用叙述代替真实调用"
_NARRATIVE_VERB_RE = re.compile(
    r"(?:调用|执行|运行|使用|发起|请求|通过|借助|用)\s*|"
    r"\b(?:call|invoke|run|execute|use|using|via)\s+",
    re.IGNORECASE,
)


def user_asks_plan(text: str) -> bool:
    """用户最新消息本身是否在要"计划/方案"(第一轮计划收尾放行依据)。"""
    if not text:
        return False
    masked, _ = _code_masked(text)
    return bool(PLAN_ASK_RE.search(masked))


def has_narrative_execution(text: str, tool_names: list) -> bool:
    """叙述式执行检测: 输出中出现【执行动词 + 已声明工具名】的共现
    (如"我调用 read_file 读取了…"/"execute run_cmd to…"),却没有任何真实 <tool_call> 标签
    —— 历史中的调用不会因叙述而发生,这是典型假执行。
    代码围栏内(示例/文档)不误报;未声明工具名的泛泛描述("调用工具")不误报。"""
    if not text or not tool_names:
        return False
    masked, _ = _code_masked(text)
    verb = f"(?:{_NARRATIVE_VERB_RE.pattern})"  # 交替分支必须整体包裹,否则动词单支脱离工具名约束(误报根因)
    for name in tool_names:
        n = re.escape(str(name))
        # 模式1: 动词 ... 工具名(中间允许"XX 工具/命令"等 16 字符内的间隔)
        if re.search(rf"{verb}[^.。!\n]{{0,16}}\b{n}\b", masked, re.IGNORECASE):
            return True
        # 模式2: 工具名 ... 了(中文完成态紧随工具名,如 "read_file 读取了")。
        # F5 收紧: 排除"支持/允许/可/用于 进行"式的能力讨论("支持进行"≠执行动作)。
        if re.search(rf"\b{n}\b(?![^.。!\n]{{0,12}}(?:支持|允许|可用于|适合))[^.。!\n]{{0,12}}(?:了|并|来|进行)", masked):
            return True
    return False


def has_completion_claim(text: str) -> bool:
    """谎报完成检测: 文本声称任务完成(无过去式证据约束,交由调用方结合'本轮有无工具调用'判断)。"""
    if not text:
        return False
    masked, _ = _code_masked(text)
    return bool(COMPLETION_CLAIM_RE.search(masked))
# 段落终止符:空行 / 说话人标记 / 代码围栏(用于截断"伪造结果段落"的贪婪删除)
_FAKE_PARA_STOP_RE = re.compile(r"\n\s*\n|\n(?:```|`)|(?:\n|\A)(?:Human|System|User|Assistant)\s*[:：]")


def build_tool_choice_directive(tool_choice, tool_names: list) -> str:
    """F3: 把 OpenAI 的 tool_choice 翻译为协议内强制指令。
    - "required" → 本回合必须以 <tool_call> 结束,禁止纯文本
    - {"type":"function","function":{"name":X}} → 必须调用名为 X 的工具(参数可自行判断)
    - "auto"/None/其他 → 返回空串(不加约束,即默认行为)
    未声明的工具名 → 返回空串并在层上由调用方 400(协议层无法表达"强制调用不存在的工具")。"""
    if tool_choice in (None, "auto", "none"):
        return ""
    try:
        if tool_choice == "required":
            return ("\n\n[本回合强制工具调用]\n客户端要求本回合必须进行工具调用:你必须输出至少一个 "
                    "<tool_call> 块作为回合的结束,严禁只输出纯文本或计划。")
        if isinstance(tool_choice, dict):
            fn = tool_choice.get("function") or {}
            name = fn.get("name") or tool_choice.get("name")
            if name and name in (tool_names or []):
                return (f"\n\n[本回合强制工具调用]\n客户端要求本回合必须调用工具 {name}:"
                        f"请输出一个 <tool_call> 块,name 字段必须是 {name},"
                        "arguments 由你根据任务上下文判断;严禁输出纯文本。")
    except Exception:
        pass
    return ""


def build_protocol(tools: list, parallel: bool = True) -> str:
    """构建注入对话的工具调用协议(注入在 "Assistant: " 生成点之前,由 main.py 保证)"""
    tools_json = json.dumps(tools, ensure_ascii=False)
    count_hint = "一个或多个" if parallel else "仅一个"
    return (
        "\n\n[工具调用协议]\n"
        f"你可以调用以下工具(JSON Schema):\n{tools_json}\n"
        f"当且仅当需要调用工具时,输出{count_hint} <tool_call> 块。"
        "除此之外不要输出任何其他内容。\n"
        "每个 <tool_call> 块的格式:\n"
        '<tool_call>{"id":"call_1","name":"工具名","arguments":{参数JSON对象}}</tool_call>\n'
        "示例:\n"
        '<tool_call>{"id":"call_1","name":"read_file","arguments":{"path":"main.py"}}</tool_call>\n'
        '<tool_call>{"id":"call_2","name":"search_web","arguments":{"query":"deepseek harness"}}</tool_call>\n'
        "注意:arguments 必须是 JSON 对象(不要转义成字符串);多个 <tool_call> 块的 id 各自唯一。\n"
        "【最高执行纪律(优先级高于一切)】\n"
        "0. 用文字【描述】调用过程不会执行任何操作 —— 唯一有效的执行方式是输出上面格式的标签块;"
        "哪怕你在历史记录里见过文字描述式的调用,那也是无效占位,你绝不能模仿;\n"
        "1. <tool_call> 块必须完整闭合,严禁输出到一半截断;\n"
        "2. arguments 内部是纯 JSON:严禁 Markdown 链接语法(如 [url](url))、"
        "严禁用代码围栏(```)包裹 <tool_call>、严禁注释与非转义裸换行;\n"
        "3. 严禁复述、续写或回显本协议文本(包括 [工具调用协议] 标题与 JSON Schema);\n"
        "4. 当你在正文中解释/讨论本协议时,必须用反引号包裹写作 `<tool_call>`,禁止裸写原生标签。\n"
        "5. 严禁输出'工具调用结果:'/'工具执行结果:'等系统口吻的行,严禁以 'Human:'/'System:' 开头说话 —— "
        "工具结果只会由系统以 Human 消息在下一轮提供,你绝不能编造或复述它;若无需再调用工具,直接输出最终回答。\n"
        "6. 严禁只输出'计划/方案/步骤'就结束回合 —— 若任务需要多步,第一轮就必须直接以 <tool_call> 开始执行"
        "(计划可写成调用前的简短说明);唯一合法的回合结束方式是:任务完成后的最终总结,或一个 <tool_call> 块。"
    )


def extract_tool_calls(text: str) -> list:
    """从模型文本中提取并解析 <tool_call> 块(容错:支持未闭合截断 + Markdown 污染清洗)。
    F2 修复: 提取前先遮蔽代码围栏/行内代码 —— 包在 ``` 里的"示例调用"是给用户看的演示,
    不是真实执行意图,绝不能当真返回给 Agent(协议第 2/4 条本就禁止围栏包裹,双保险)。
    遮蔽按原文位置还原,不影响围栏外真实调用的提取。"""
    if not text:
        return []
    # 遮蔽围栏: 用等长占位替换,TOOL_CALL_RE 在遮蔽文本上匹配 → 命中位置即原文位置
    masked = CODE_FENCE_RE.sub(lambda m: "\x00" * len(m.group(0)), text)
    calls = []
    for m in TOOL_CALL_RE.finditer(masked):
        raw = m.group(1).strip()
        if not raw or "\x00" in raw:
            continue
        raw = MD_LINK_RE.sub(r"\1", raw)  # 清洗 [url](url) 污染
        obj = None
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            obj = _repair_json(raw)
        if isinstance(obj, dict) and isinstance(obj.get("name"), str) and obj["name"]:
            calls.append(obj)
    return calls


def has_malformed_tool_call(text: str) -> bool:
    """破损/回声检测(结构指纹):非代码区域出现 <tool_call> 后紧跟 `{`(真调用,可能破损)
    或标签悬空在文本末尾,或模型回吐 [工具调用协议] 模板。
    正文自然讨论(标签后接自然语言,如"<tool_call>这个词汇")不误报。"""
    if not text:
        return False
    masked = CODE_FENCE_RE.sub(lambda m: " " * len(m.group(0)), text)  # 遮蔽代码块
    if "[工具调用协议]" in masked:
        return True
    if BROKEN_TAG_JSON_RE.search(masked) or BROKEN_TAG_EOF_RE.search(masked):
        return True
    # 成对完整闭合的 <tool_call> 已由 extract_tool_calls 处理(它修得好就修好);
    # 这里额外兜底: 开了 N 个、只关了 M 个且 N>M,且开标签后有内容跟随 —— 判破损
    opened = masked.count("<tool_call>")
    closed = masked.count("</tool_call>")
    return opened > closed and not re.search(r"<tool_call>\s*[^{\s]", masked)


def _code_masked(text: str) -> str:
    """遮蔽代码围栏与行内代码,返回(遮蔽文本, 围栏列表)供后续还原。"""
    fences: list[str] = []

    def _stash(m):
        fences.append(m.group(0))
        return f"\x00F{len(fences) - 1}\x00"

    return CODE_FENCE_RE.sub(_stash, text or ""), fences


def has_fake_tool_result(text: str) -> bool:
    """模型是否伪造了"工具调用结果:"式的系统口吻行(结果只会由系统提供,assistant 输出即伪造)。
    代码块内示例/被问及的正文不误报。"""
    if not text:
        return False
    masked, _ = _code_masked(text)
    return bool(FAKE_RESULT_RE.search(masked))


def has_role_confusion(text: str) -> bool:
    """模型是否以 Human:/System:/User: 行开头输出(角色错乱:替系统/用户说话)。"""
    if not text:
        return False
    masked, _ = _code_masked(text)
    return bool(ROLE_CONFUSION_RE.search(masked))


def has_plan_only(text: str) -> bool:
    """模型是否以"计划/方案"文本收尾(未输出任何 <tool_call>)。
    用于执行中的多步任务:纯计划收尾 = 假完成,应打回强制执行。
    v2 双通道检测:
    1. 引导词指纹(中英文变体,行首)
    2. 结构指纹: 裸任务清单(≥2 行编号/勾选项),且全文与各清单行均无过去式完成标记
       —— "1. 读取文件 2. 修改代码"是计划;"1. 修复了X 2. 更新了Y"是完成总结(放行)
    代码块内/行内代码中的示例不误报。"""
    if not text:
        return False
    masked, _ = _code_masked(text)
    if PLAN_ONLY_RE.search(masked):
        return True
    if not PLAN_PAST_RE.search(masked):
        list_lines = [ln for ln in masked.splitlines() if PLAN_LIST_RE.match(ln)]
        if len(list_lines) >= 2 and not any(_PLAN_LINE_PAST_RE.search(ln) for ln in list_lines):
            return True
    return False


def _drop_paragraphs(text: str, line_re) -> str:
    """删除所有以 line_re 开头的"段落":自匹配行起,到空行 / 说话人标记 / 代码围栏为止。
    用于清理伪造工具结果、角色错乱复述等自导自演内容(含多行负载)。
    终止符搜索从匹配行的行尾换行处开始 —— 位置 0 的 \\A 分支不可能命中同一行。"""
    out = []
    pos = 0
    for m in line_re.finditer(text):
        out.append(text[pos:m.start()])
        rest = text[m.start():]
        nl = rest.find("\n")
        search_from = nl if nl >= 0 else len(rest)
        stop = _FAKE_PARA_STOP_RE.search(rest, search_from)
        if stop:
            pos = m.start() + stop.start()
        else:
            pos = len(text)
    out.append(text[pos:])
    return "".join(out)


def _drop_fake_result_paragraphs(text: str) -> str:
    """删除伪造的"工具调用结果:"段落(自匹配行到空行/说话人标记/围栏为止)。"""
    return _drop_paragraphs(text, FAKE_RESULT_RE)


def _drop_role_confusion_paragraphs(text: str) -> str:
    """删除以 Human:/System:/User: 开头的角色错乱段落(模型替系统/用户说话并复述内容)。"""
    return _drop_paragraphs(text, ROLE_CONFUSION_RE)


def _repair_json(raw: str):
    """轻量修补:Markdown 清洗 → 平衡花括号提取 → 截断补齐闭合"""
    raw = MD_LINK_RE.sub(r"\1", raw)
    start = raw.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(raw)):
        ch = raw[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(raw[start:i + 1])
                except json.JSONDecodeError:
                    return None
    # 截断补齐:未闭合的字符串与花括号尽量补齐后重试
    if depth > 0:
        candidate = raw[start:]
        if in_str:
            candidate += '"'
        candidate += "}" * depth
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            return None
    return None


def validate_tool_calls(calls: list, tools: list) -> tuple:
    """校验并转换为 OpenAI tool_calls 格式。返回 (valid_calls, errors)"""
    declared = set()
    for t in tools or []:
        if isinstance(t, dict):
            fn = t.get("function") or {}
            if isinstance(fn, dict) and fn.get("name"):
                declared.add(fn["name"])
    valid, errors = [], []
    seen_ids = set()
    for c in calls:
        name = c.get("name")
        args = c.get("arguments")
        cid = c.get("id") or f"call_{uuid.uuid4().hex[:10]}"
        if cid in seen_ids:
            cid = f"call_{uuid.uuid4().hex[:10]}"
        seen_ids.add(cid)
        if not name:
            errors.append("存在缺少 'name' 字段的 <tool_call>")
            continue
        if declared and name not in declared:
            errors.append(f"工具 '{name}' 未在 tools 中声明")
            continue
        if not isinstance(args, dict):
            errors.append(f"工具 '{name}' 的 arguments 不是 JSON 对象")
            continue
        valid.append({
            "id": cid,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
        })
    return valid, errors


def strip_tag_blocks(text: str) -> str:
    """剥除 <tool_call> 块(含未闭合截断)与协议回声,保留计划/说明文本。
    代码块/行内代码先 stash 后还原 —— 正文合法演示格式时不误剥内容。"""
    if not text:
        return ""
    masked, fences = _code_masked(text)
    masked = re.sub(r"<tool_call>[\s\S]*?(?:</tool_call>|$)", "", masked)
    masked = PROTOCOL_ECHO_RE.sub("", masked)
    masked = re.sub(r"\n{3,}", "\n\n", masked).strip()
    return _restore_fences(masked, fences)


def sanitize_assistant_text(text: str) -> str:
    """净化 assistant 正文(工具路径 final 文本生产器;也是"伪造工具结果外泄"的第二道闸):
    1. stash 代码围栏(示例免误伤) → 2. 剥 <tool_call> 块与协议回声
    3. 删除伪造的"工具调用结果:"回显段落(含多行被复述负载)
    4. 删除角色错乱行(以 Human:/System:/User: 开头 —— 模型替系统/用户说话)
    5. 还原代码围栏,压缩多余空行。
    与 strip_tag_blocks 的区别: 多清理"模型自导自演工具结果/错乱角色"两类泄漏。"""
    if not text:
        return ""
    masked, fences = _code_masked(text)
    masked = re.sub(r"<tool_call>[\s\S]*?(?:</tool_call>|$)", "", masked)
    masked = PROTOCOL_ECHO_RE.sub("", masked)
    masked = _drop_fake_result_paragraphs(masked)
    # 角色错乱行(模型替系统/用户说话)整段删除 —— 不能只删前缀留负载
    masked = _drop_role_confusion_paragraphs(masked)
    masked = re.sub(r"\n{3,}", "\n\n", masked).strip()
    return _restore_fences(masked, fences)


def _restore_fences(masked: str, fences: list) -> str:
    def _unstash(m):
        try:
            return fences[int(m.group(1))]
        except (IndexError, ValueError):
            return ""

    return re.sub(r"\x00F(\d+)\x00", _unstash, masked)


def feedback_text(errors: list, level: int = 0) -> str:
    """构造打回重试的修正消息正文。level 升级措辞(0 提醒 → 1 警告 → 2 最后通牒)。
    修复: 旧版 3 次打回同一文案,模型固执时 3 连败后计划文本被当最终回复放行。"""
    base = (
        "你的上一次输出格式不正确: " + ("; ".join(errors) if errors else "未解析到有效的 <tool_call> 块") + "。\n"
        "请重新输出,严格遵守 <tool_call> 格式(JSON 对象,不要额外文字),不要调用未声明的工具。\n"
        "特别提醒:严禁复述 [工具调用协议] 模板;标签必须完整闭合;"
        "arguments 内严禁 Markdown 链接语法(如 [url](url));"
        "严禁输出'工具调用结果:'等工具结果口吻(结果只会由系统以 Human 消息提供,你不得编造或复述);"
        "严禁只做计划/方案就停下 —— 需要执行时本轮必须直接输出 <tool_call> 开始干活;"
        "用文字描述调用过程不等于执行,唯一有效方式是输出标签块。"
    )
    if level <= 0:
        return base
    if level == 1:
        return (
            "【第 2 次警告】你上一轮仍然只输出了计划/步骤文字,没有真正执行。任务尚未完成。\n"
            + base
            + "\n再次只输出计划将被判定为任务失败。"
        )
    return (
        "【最后通牒】你已连续多轮只输出计划而未执行。现在必须立即输出一个 <tool_call> 块,格式示例:\n"
        '<tool_call>{"name":"工具名","arguments":{"参数":"值"}}</tool_call>\n'
        "本回合必须以工具调用结束,不允许任何计划、步骤列表或说明文字。"
        "若确实无工具可调,才允许输出纯文本,但必须以真实执行结果为依据。"
    )


def append_feedback(conversation: str, errors: list, level: int = 0) -> str:
    """把打回修正消息注入到对话的正确位置:
    剥掉末尾生成点("Assistant: ") → 追加 Human 反馈 → 重新以生成点收尾。
    绝不把反馈文本放在生成点之后(否则又成为"续写素材",诱发回声)。
    level: 升级措辞强度(0/1/2),由 main.py 按 attempt 序号递增传入。"""
    tail = "Assistant: "
    base = conversation
    if base.endswith(tail):
        base = base[: -len(tail)]
    return (
        base
        + "\n\nHuman: [工具调用协议修正]\n"
        + feedback_text(errors, level)
        + "\n\n"
        + tail
    )


def serialize_assistant_tool_calls(msg) -> str:
    """把带 tool_calls 的 assistant 消息转成对话文本(供 Gemini 看历史)。
    关键: 用【原生标签格式】回放历史 —— 历史样例是模型最强的模仿对象,
    若用散文式"助手调用了工具[...]",长会话会诱发模型用叙述代替真实调用(连续失败的根因之一)。"""
    parts = []
    if getattr(msg, "content", None):
        parts.append(str(msg.content))
    for tc in (getattr(msg, "tool_calls", None) or []):
        fn = tc.get("function", {}) if isinstance(tc, dict) else {}
        name = fn.get("name", "?") if isinstance(fn, dict) else "?"
        args = fn.get("arguments", "") if isinstance(fn, dict) else ""
        if isinstance(args, str):
            args_payload = args
        else:
            args_payload = json.dumps(args, ensure_ascii=False)
        try:
            args_obj = json.loads(args_payload) if isinstance(args_payload, str) else args_payload
        except (json.JSONDecodeError, TypeError):
            args_obj = args_payload
        payload = {"id": tc.get("id") or f"call_h{uuid.uuid4().hex[:6]}", "name": name, "arguments": args_obj}
        parts.append(f'<tool_call>{json.dumps(payload, ensure_ascii=False)}</tool_call>')
    return "\n".join(parts)


def serialize_tool_result(msg, label: str = "") -> str:
    """把 role=tool 的消息转成对话文本。
    修复(根因): 旧格式输出裸行 "工具调用结果: ...",夹在两条 Assistant 轮之间无任何
    归属标记 —— 模型会把它学成"assistant 正文允许出现工具结果口吻"的示范,
    进而在自己续写时伪造 "工具调用结果:" 行(工具结果被当成正文抛出的根因)。
    新格式显式标记为 Human(他人/系统)消息并声明只读禁复述,杜绝模仿源。
    F8: label 由调用方(prepare_conversation)按 tool_call_id 回查工具名后传入,
    并行多调用时结果与调用一一对应,不再靠顺序隐式关联。"""
    content = getattr(msg, "content", "")
    if isinstance(content, list):
        content = json.dumps(content, ensure_ascii=False)
    tag = f"({label}) " if label else ""
    return f"Human: 工具执行结果{tag}(系统提供,只读,请勿复述): {content}"
