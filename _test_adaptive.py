# -*- coding: utf-8 -*-
"""自适应剪裁(最后手段: 最后一条消息内容截断)单元测试"""
import sys
sys.path.insert(0, r"F:\DevEnv\py312_new\Gemini-API_old")


class Msg:
    def __init__(self, role, content, tool_calls=None):
        self.role = role
        self.content = content
        self.tool_calls = tool_calls


import main as m

PROTO_RESERVE = 60000
BUDGET = m.CTX_CHECK_THRESHOLD - PROTO_RESERVE  # 920000

ok = True
results = []


def T(name, cond, detail=None):
    results.append((name, bool(cond), detail))
    return bool(cond)


# 场景1: 最后一条 tool 结果 100 万 → 历史删光 + 最后一条截断 → 压回预算
msgs = [Msg("system", "S" * 2000), Msg("user", "帮我分析"),
        Msg("assistant", "已读取"), Msg("tool", "X" * 1000000)]
t, dropped = m._degrade_messages_for_budget(msgs, BUDGET)
est = sum(m._estimate_message_chars(x) for x in t)
T("场景1 巨型tool结果压回预算", est <= BUDGET, (est, BUDGET))
T("场景1 确实发生裁剪", dropped > 0, dropped)
last = t[-1]
T("场景1 最后一条被截断(含提示)", isinstance(last.content, str) and "已自适应截断" in last.content)
T("场景1 保留开头内容", isinstance(last.content, str) and last.content.startswith("X"))
T("场景1 system 保留", t[0].content == "S" * 2000)

# 场景2: 最后一条 assistant 超长文本
msgs2 = [Msg("system", "S" * 2000), Msg("user", "写长文"), Msg("assistant", "Z" * 1000000)]
t2, _ = m._degrade_messages_for_budget(msgs2, BUDGET)
est2 = sum(m._estimate_message_chars(x) for x in t2)
T("场景2 巨型assistant文本压回预算", est2 <= BUDGET, est2)
T("场景2 截断提示存在", "已自适应截断" in t2[-1].content)

# 场景3: 大历史 + 小结尾 → 常规裁剪(不截断最后一条,内容原样)
msgs3 = [Msg("system", "S" * 2000)] + [Msg("user", "U" * 20000) for _ in range(50)] + [Msg("user", "现在几点")]
t3, d3 = m._degrade_messages_for_budget(msgs3, BUDGET)
est3 = sum(m._estimate_message_chars(x) for x in t3)
T("场景3 常规裁剪压回预算", est3 <= BUDGET, est3)
T("场景3 最后一条原样保留", t3[-1].content == "现在几点")
T("场景3 未触发内容截断", all("已自适应截断" not in str(x.content) for x in t3))

# 场景4: 最后一条是 tool 结果 90 万(其父 tool_calls 保留) → 常规裁剪即可压回
tc = [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{\"a\":1}"}}]
msgs4 = [Msg("system", "S" * 2000), Msg("user", "开头问题"),
         Msg("assistant", "中间回答" * 100),
         Msg("assistant", "调工具", tool_calls=tc),
         Msg("tool", "R" * 900000)]
t4, _ = m._degrade_messages_for_budget(msgs4, BUDGET)
est4 = sum(m._estimate_message_chars(x) for x in t4)
T("场景4 大tool结果压回预算", est4 <= BUDGET, est4)
# tool 结果与父 tool_calls 成对保留或一起删: 不得出现"结果在而父调用没了"
pairs_ok = True
for idx, x in enumerate(t4):
    if x.role == "tool":
        prev = t4[idx - 1] if idx else None
        if not (prev and prev.role == "assistant" and prev.tool_calls):
            pairs_ok = False
T("场景4 tool结果与父调用成对", pairs_ok)

# 场景5: 小对话 → 原样返回
msgs5 = [Msg("system", "hi"), Msg("user", "你好")]
t5, d5 = m._degrade_messages_for_budget(msgs5, BUDGET)
T("场景5 小对话不动", t5 is msgs5 and d5 == 0)

# 场景6: 幂等 — 二次裁剪不再变化
t1b, d1b = m._degrade_messages_for_budget(t, BUDGET)
est1b = sum(m._estimate_message_chars(x) for x in t1b)
T("场景6 幂等(二次裁剪不再变化)", est1b <= BUDGET and d1b == 0, (est1b, d1b))

# 场景7: 巨型 tool_calls 链(最后一条 assistant+tool_calls 自身超预算) → 整链删除
msgs7 = [Msg("system", "S" * 2000), Msg("user", "开头问题"),
         Msg("assistant", "X" * 1200000, tool_calls=tc),
         Msg("tool", "R" * 100)]
t7, _ = m._degrade_messages_for_budget(msgs7, BUDGET)
est7 = sum(m._estimate_message_chars(x) for x in t7)
T("场景7 巨型tool_calls链整链删除", est7 <= BUDGET and not any(x.role == "tool" for x in t7), est7)

import json
for name, passed, detail in results:
    print(f"[{'PASS' if passed else 'FAIL'}] {name}" + (f"  detail={detail}" if detail is not None else ""))
    ok = ok and passed
print("=" * 40)
print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
