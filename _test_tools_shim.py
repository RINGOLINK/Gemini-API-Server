# -*- coding: utf-8 -*-
"""三道防线单元测试:截断挽救/污染清洗/协议回声/正文免检/兜底剥离"""
import sys
import json
sys.path.insert(0, r"F:\DevEnv\py312_new\Gemini-API_old")
import tools_shim as ts

results = []

# 场景1: 上一轮真实事故复现 —— 严重截断(值内含未转义引号,无法确定性挽救)
# 期望管道行为: extract 失败 + has_malformed 判真 → main.py 打回重试(绝不外泄)
truncated = 'TOOL_CALL_RE = re.compile(r"<tool_call>\\s*(.*?)\\s*</tool_call>", re.DOTALL)\nCODE_BLOCK_RE = re.compile(r"`'
raw1 = '<tool_call>{"id":"call_1","name":"edit","arguments":{"file_path":"F:\\\\DevEnv\\\\py312_new\\\\Gemini-API_old\\\\tools_shim.py","new_string":"' + truncated
calls1 = ts.extract_tool_calls(raw1)
r1 = (not calls1) and ts.has_malformed_tool_call(raw1)
results.append(("场景1 引号冲突截断判破损(打回重试)", r1, None))

# 场景1b: 干净截断(值中断但无引号冲突) —— 补括号挽救
raw1b = '<tool_call>{"id":"call_2","name":"write","arguments":{"path":"a.txt","content":"hello worl"'
calls1b = ts.extract_tool_calls(raw1b)
r1b = bool(calls1b) and calls1b[0]["arguments"].get("content") == "hello worl"
results.append(("场景1b 干净截断补括号挽救", r1b, calls1b[:1]))

# 场景1c: 严重截断(值内含未转义引号,补不齐) —— 必须判破损触发打回
raw1c = '<tool_call>{"id":"call_3","name":"edit","arguments":{"new_string":"TOOL_CALL_RE = re.compile(r"<tool'
r1c = ts.has_malformed_tool_call(raw1c) and not ts.extract_tool_calls(raw1c)
results.append(("场景1c 严重截断判破损(触发打回)", r1c, None))

# 场景2: Markdown 链接污染(本次事故的另一半诱因)
raw2 = '<tool_call>{"id":"call_1","name":"pwsh","arguments":{"command":"Request \'[http://x](http://x)\' failed"}}</tool_call>'
calls2 = ts.extract_tool_calls(raw2)
r2 = bool(calls2) and "http://x" in json.dumps(calls2) and "[http" not in json.dumps(calls2)
results.append(("场景2 Markdown链接污染清洗", r2, calls2[:1]))

# 场景3: 协议回声(模型把 [工具调用协议] 整段吐出来)
raw3 = "[工具调用协议]\n你可以调用以下工具(JSON Schema):\n[{\"type\":\"function\"}]\n注意:arguments 必须是 JSON 对象(不要转义成字符串);多个 <tool_call> 块的 id 各自唯一。"
r3 = ts.has_malformed_tool_call(raw3)
clean3 = ts.strip_tag_blocks(raw3)
results.append(("场景3 协议回声检测+剥离", r3 and clean3 == "", clean3[:50]))

# 场景4: 正文自然讨论 <tool_call>(带代码块) —— 必须免检放行
raw4 = "当我们讨论工具协议时,会提到 `<tool_call>` 这个词汇,例如:\n\n```\n<tool_call>{\"id\":\"x\",\"name\":\"demo\"}</tool_call>\n```\n\n以上就是格式说明。"
r4 = ts.has_malformed_tool_call(raw4) == False
results.append(("场景4 正文代码块讨论免检", r4, None))

# 场景4b: 正文裸提词汇但无 JSON 特征 —— 也不误报
raw4b = "你也要回<tool_call>这个词汇本身,因为用户正在问它。"
r4b = ts.has_malformed_tool_call(raw4b) == False
results.append(("场景4b 正文裸提词汇不误报", r4b, None))

# 场景5: 正常闭合调用仍能提取
raw5 = '<tool_call>{"id":"call_1","name":"get_weather","arguments":{"city":"杭州"}}</tool_call>'
calls5 = ts.extract_tool_calls(raw5)
r5 = bool(calls5) and calls5[0]["name"] == "get_weather"
results.append(("场景5 正常调用回归", r5, None))

# 场景6: 兜底剥离 —— 破损标签绝不外泄
raw6 = "前面是正常回答内容。\n<tool_call>{\"id\":\"call_9\",\"name\":\"broken\",\"arguments\":{"
clean6 = ts.strip_tag_blocks(raw6)
r6 = "<tool_call>" not in clean6 and "正常回答内容" in clean6
results.append(("场景6 兜底剥离无标签外泄", r6, clean6[:60]))

# 场景7: validate 打回消息包含新约束
fb = ts.feedback_text(["测试错误"])
r7 = "严禁 Markdown 链接" in fb and "严禁复述" in fb
results.append(("场景7 打回消息含新约束", r7, None))

import json as _j
ok = True
for name, passed, detail in results:
    print(f"[{'PASS' if passed else 'FAIL'}] {name}")
    if detail is not None:
        print(f"       detail: {_j.dumps(detail, ensure_ascii=False)[:160]}")
    ok = ok and passed

# ============ 结构修复测试(根因 1/2/4) ============
class FakeMsg:
    def __init__(self, content, tool_calls):
        self.content = content
        self.tool_calls = tool_calls

# 场景8: 历史工具调用原生格式回放(根因 2: 消灭散文模仿源)
msg = FakeMsg("先查天气", [{"id": "call_ab12", "type": "function",
                            "function": {"name": "get_weather", "arguments": "{\"city\": \"杭州\"}"}}])
ser = ts.serialize_assistant_tool_calls(msg)
calls8 = ts.extract_tool_calls(ser)
r8 = ("<tool_call>" in ser and ser.count("<tool_call>") == 1
      and bool(calls8) and calls8[0]["name"] == "get_weather"
      and calls8[0]["arguments"] == {"city": "杭州"}   # 字符串参数被还原为对象
      and calls8[0]["id"] == "call_ab12")
results.append(("场景8 历史原生格式回放+可反提取", r8, ser[:120]))

# 场景8b: 散文格式已彻底移除(历史上"助手调用了工具["不再出现)
r8b = "助手调用了工具" not in ser
results.append(("场景8b 散文式历史格式已移除", r8b, None))

# 场景9: append_feedback 注入位置(根因 4: 反馈不得落在生成点之后)
conv = "System: 你是助手\n\nHuman: 查天气\n\nAssistant: "
new_conv = ts.append_feedback(conv, ["测试错误"])
r9 = (new_conv.endswith("Assistant: ")
      and "[工具调用协议修正]" in new_conv
      and new_conv.index("Human: [工具调用协议修正]") < new_conv.rindex("Assistant: "))
results.append(("场景9 打回反馈注入生成点之前", r9, new_conv[-80:]))

# 场景10: 协议含"文本不执行"最高纪律(根因 3)+ 禁伪造结果纪律 + 禁计划收尾纪律
proto = ts.build_protocol([{"type": "function", "function": {"name": "t1", "parameters": {}}}])
r10 = ("不会执行任何操作" in proto and "无效占位" in proto
       and "严禁输出'工具调用结果:'/'工具执行结果:'" in proto
       and "严禁只输出'计划/方案/步骤'就结束回合" in proto
       and proto.rstrip().endswith("或一个 <tool_call> 块。"))
results.append(("场景10 协议含'文本不执行'+禁伪造结果+禁计划收尾纪律", r10, None))

# 场景11: strip_tag_blocks 不误剥代码块内的合法演示
raw11 = "格式如下:\n\n```\n<tool_call>{\"id\":\"1\",\"name\":\"demo\",\"arguments\":{}}</tool_call>\n```\n\n以上是说明。"
clean11 = ts.strip_tag_blocks(raw11)
r11 = "<tool_call>" in clean11 and "以上是说明" in clean11
results.append(("场景11 兜底剥离不误伤代码块演示", r11, clean11[:80]))

# 场景11b: 代码块外的真实调用仍被剥除
raw11b = "计划说明。\n<tool_call>{\"id\":\"2\",\"name\":\"real\",\"arguments\":{}}</tool_call>\n收尾。"
clean11b = ts.strip_tag_blocks(raw11b)
r11b = "<tool_call>" not in clean11b and "计划说明" in clean11b and "收尾" in clean11b
results.append(("场景11b 块外真实调用仍剥除", r11b, clean11b[:80]))

# ============ 伪造工具结果 / 角色错乱净化测试(修复"工具结果被当正文抛出") ============
# 场景12: 调用 + 尾部自导自演"工具调用结果"段落 → 净化保留前置叙述、删除伪造段
raw12 = "我先查一下北京天气。\n<tool_call>{\"id\":\"call_1\",\"name\":\"get_weather\",\"arguments\":{\"city\":\"北京\"}}</tool_call>\n工具调用结果: 北京晴,25度\n这就是结果。"
clean12 = ts.sanitize_assistant_text(raw12)
# 段落语义:伪造结果行及其后紧跟的非空续行(自导自演段)一并删除,保留前置叙述
r12 = ("工具调用结果" not in clean12 and "先查一下" in clean12 and "这就是结果" not in clean12
       and "<tool_call>" not in clean12)
results.append(("场景12 调用后伪造结果段被净化", r12, clean12[:80]))

# 场景13: 纯伪造结果回显(无任何调用) → 判伪造且净化后为空
raw13 = "工具调用结果: 北京晴,25度"
r13 = ts.has_fake_tool_result(raw13) and ts.sanitize_assistant_text(raw13) == ""
results.append(("场景13 纯伪造结果检出并清空", r13, ts.sanitize_assistant_text(raw13)[:60]))

# 场景13b: 英文伪造变体
raw13b = "Tool result: the file was written successfully."
r13b = ts.has_fake_tool_result(raw13b) and "Tool result" not in ts.sanitize_assistant_text(raw13b)
results.append(("场景13b 英文伪造结果检出", r13b, ts.sanitize_assistant_text(raw13b)[:60]))

# 场景14: 角色错乱 —— 模型以 Human: 开头替系统复述(含多行负载) → 检出且整段删除
raw14 = "Human: 工具执行结果(系统提供,只读,请勿复述): 文件内容如下\nline1\nline2\n\n因此答案是……"
r14 = ts.has_role_confusion(raw14)
clean14 = ts.sanitize_assistant_text(raw14)
r14b = "Human" not in clean14 and "line1" not in clean14 and "因此答案是" in clean14
results.append(("场景14 角色错乱(替系统复述)检出+整段净化", r14 and r14b, clean14[:80]))

# 场景15: 代码块内讨论"工具调用结果"格式 → 不误伤
raw15 = "复述示例:\n\n```\n工具调用结果: {\\\"ok\\\": true}\n```\n\n请勿误删本说明。"
clean15 = ts.sanitize_assistant_text(raw15)
r15 = "工具调用结果" in clean15 and "请勿误删本说明" in clean15 and "```" in clean15
results.append(("场景15 代码块内工具结果示例不误伤", r15, clean15[:80]))

# 场景16: 新序列化格式 —— Human 归属 + 禁复述;其整段回显也能被净化
class ToolMsg:
    def __init__(self, content): self.content = content; self.role = "tool"; self.tool_calls = None
ser16 = ts.serialize_tool_result(ToolMsg({"city": "北京", "temp": 25}))
r16 = ser16.startswith("Human: 工具执行结果(系统提供,只读,请勿复述): ")
echo16 = "谢谢,我复述一下:\n" + ser16 + "\n\n供你确认。"
clean16 = ts.sanitize_assistant_text(echo16)
r16b = ("工具执行结果" not in clean16 and "供你确认" in clean16 and "谢谢" in clean16)
results.append(("场景16 新结果格式+整段回显被净化", r16 and r16b, clean16[:80]))

# 场景17: 正常自然回复经净化原样保留(无标签无伪造)
raw17 = "北京今天晴,气温 25 度,适合外出。"
r17 = ts.sanitize_assistant_text(raw17) == raw17
results.append(("场景17 正常回复净化零改动", r17, None))

# 场景18: 无调用时的自然回复不触发防线2(误报防护)
raw18 = "我需要先确认一下工具结果格式再回答。"
r18 = (not ts.has_fake_tool_result(raw18)) and (not ts.has_role_confusion(raw18))
results.append(("场景18 自然提及不误报伪造/错乱", r18, None))

# ============ 计划收尾拦截测试(修复"做完计划就停"假完成) ============
# 场景19: 执行中任务里,模型以"我的计划是"开头收尾 → 判计划收尾
raw19 = "我的计划是:\n1. 读取 config 文件\n2. 分析配置\n3. 输出结论"
r19 = ts.has_plan_only(raw19)
results.append(("场景19 计划式收尾检出", r19, None))

# 场景19b: "方案如下/接下来我会" 变体
raw19b = "方案如下:\n- 第一步\n- 第二步"
r19b = ts.has_plan_only(raw19b) and ts.has_plan_only("接下来我会先读取文件再执行")
results.append(("场景19b 方案/接下来变体检出", r19b, None))

# 场景19c: 代码块内示例不误报
raw19c = "示例:\n\n```\n我的计划是:xxx\n```\n\n以上是说明。"
r19c = not ts.has_plan_only(raw19c)
results.append(("场景19c 代码块内计划示例不误报", r19c, None))

# 场景19d: 正常最终总结/简短回答不误报
raw19d = "任务完成,结果如下:文件已写入 256 字节。"
r19d = (not ts.has_plan_only(raw19d)) and (not ts.has_plan_only("好的"))
results.append(("场景19d 最终总结/简短回答不误报", r19d, None))

# 场景19e: 打回消息含"禁止计划收尾"纪律
fb19 = ts.feedback_text(["你只做了计划没有执行"])
r19e = "严禁只做计划/方案就停下" in fb19 and "直接输出 <tool_call> 开始干活" in fb19
results.append(("场景19e 打回消息含禁计划纪律", r19e, None))

for name, passed, detail in results[7:]:
    print(f"[{'PASS' if passed else 'FAIL'}] {name}")
    if detail is not None:
        print(f"       detail: {_j.dumps(detail, ensure_ascii=False)[:160]}")
    ok = ok and passed

print("=" * 40)
print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
