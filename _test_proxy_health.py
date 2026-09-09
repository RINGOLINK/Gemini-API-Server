# -*- coding: utf-8 -*-
"""代理池健康探测单元测试: 状态机 / 判死排除 / 失败归因 / 恢复联动(不发真实网络)"""
import sys, time
sys.path.insert(0, r"F:\DevEnv\py312_new\Gemini-API_old")

ok = True
results = []
def T(name, cond, detail=None):
    results.append((name, bool(cond), detail)); return bool(cond)

import main as m

PX = "socks5h://127.0.0.1:59901"  # 假代理 URL(不发真实请求,只测状态机)

class St(dict):
    """最小账号状态桩"""
    def __init__(self, pid, proxy):
        super().__init__(pid=pid, proxy=proxy, status="ok", client=object(),
                         quota={"remaining": 500}, isolated=False, mode="proxy",
                         stall_count=0, last_stall_at="", last_error="")

saved_pool, saved_health, saved_suspects = m._pool, m._proxy_health, m._patrol_suspects
try:
    m._pool.clear(); m._proxy_health.clear(); m._patrol_suspects.clear()

    # ── 状态机: 未知→alive, 连败判死, 一次成功恢复 ──
    m._proxy_record_result(PX, False, None, "ConnectTimeout")
    st1 = m._probe_state(PX)
    T("1次失败: 仍 alive(未达阈值)", st1["alive"] is True and st1["fails"] == 1, st1)
    m._proxy_record_result(PX, False, None, "ConnectTimeout")
    st2 = m._probe_state(PX)
    T("2次失败: 判死(alive=False, down_since 记录)", st2["alive"] is False and st2["down_since"], st2)
    m._proxy_record_result(PX, True, 350.0)
    st3 = m._probe_state(PX)
    T("成功恢复: alive=True + rtt 回填", st3["alive"] is True and st3["rtt_ms"] == 350, st3)
    m._proxy_record_result(PX, False, None, "x")
    T("恢复后再失败: 1次不判死", m._probe_state(PX)["alive"] is True)

    # ── 直连恒不判死 ──
    m._proxy_record_result("", False, None, "x"); m._proxy_record_result("", False, None, "x")
    T("空URL(直连)不受状态机影响", m._proxy_down("") is False)

    # ── 路由排除: 判死代理下的账号不参与选择 ──
    a = St("accA", PX)          # 挂在死代理上
    b = St("accB", "socks5h://127.0.0.1:59902")  # 健康代理
    m._pool["accA"] = m._health_init(a)
    m._pool["accB"] = m._health_init(b)
    m._proxy_health.clear()
    m._proxy_record_result("socks5h://127.0.0.1:59902", True, 200.0)
    m._proxy_record_result(PX, False, None, "e"); m._proxy_record_result(PX, False, None, "e")
    best = m._pick_ready_healthiest()
    cands = m._ready_candidates()
    T("判死代理账号被路由排除", best == "accB" and [p for p, _ in cands] == ["accB"], (best, cands))

    # ── 失败归因: 代理判死期间业务失败不计账号错误链 ──
    m._account_record_failure("accA", "error")
    m._account_record_failure("accA", "error")
    T("判死期失败不计错误链", m._pool["accA"]["_err_streak"] == 0, m._pool["accA"]["_err_streak"])
    m._account_record_failure("accB", "error")
    T("健康代理账号正常计错误链", m._pool["accB"]["_err_streak"] == 1)

    # ── 恢复联动: 代理恢复 → 清该代理下账号错误链/熔断 ──
    m._pool["accA"]["_err_streak"] = 3  # 模拟判死前遗留
    m._pool["accA"]["_circuit"] = "open"; m._pool["accA"]["_circuit_until"] = time.time() + 600
    m._proxy_record_result(PX, True, 400.0)  # 恢复
    aa = m._pool["accA"]
    T("恢复联动: 错误链清零", aa["_err_streak"] == 0, aa["_err_streak"])
    T("恢复联动: 熔断关闭", aa["_circuit"] == "closed")
    T("恢复联动: 嫌疑计数清除", "accA" not in m._patrol_suspects)
    T("恢复后账号回池参与路由", "accA" in [p for p, _ in m._ready_candidates()])

    # ── 快照结构(先再判死 PX,验证绑定映射的 down 标记准确性) ──
    m._proxy_record_result(PX, False, None, "e"); m._proxy_record_result(PX, False, None, "e")
    snap = m._proxy_health_snapshot()
    abind = {b["pid"]: b for b in snap["bindings"]}
    T("快照: proxies/bindings 结构完整", len(snap["proxies"]) >= 2 and len(snap["bindings"]) == 2)
    T("快照: 绑定 down 标记准确", abind["accA"]["down"] is True and abind["accB"]["down"] is False, snap["bindings"])
finally:
    m._pool, m._proxy_health, m._patrol_suspects = saved_pool, saved_health, saved_suspects

for name, passed, detail in results:
    print(f"[{'PASS' if passed else 'FAIL'}] {name}" + (f"  detail={detail}" if detail is not None else ""))
    ok = ok and passed
print("=" * 40)
print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
