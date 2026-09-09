# -*- coding: utf-8 -*-
"""Step1 健康分 + 熔断状态机单元测试"""
import sys, time
sys.path.insert(0, r"F:\DevEnv\py312_new\Gemini-API_old")
import main as m

ok = True
results = []


def new_st(pid="t1", rem=500, ttfb=None, streak=0, stalls=0):
    st = {"pid": pid, "psid": "s", "psidts": "t", "proxy": None, "client": object(),
          "quota": {"remaining": rem, "pct": None, "reset_at": ""},
          "status": "ok", "last_error": "", "mode": "direct",
          "stall_count": stalls, "last_stall_at": ""}
    m._health_init(st)
    if ttfb is not None:
        for _ in range(5):
            st["_ttfbs"].append(ttfb)
    st["_err_streak"] = streak
    st["_stalls_recent"] = stalls
    return st


def ladder3_ok():
    """第 3 次重开应走 600s 档(阶梯封顶)"""
    st2 = new_st("l3", rem=500)
    st2["_circuit"] = "open"
    st2["_circuit_until"] = time.time() - 1
    st2["_circuit_opens"] = 2
    st2["_err_streak"] = 3   # 半开探针失败时错误链已 ≥ 阈值(此前多次失败导致熔断)
    m._pool["l3"] = st2
    m._account_record_failure("l3", "error")
    return (st2["_circuit_until"] - time.time()) > 500 and st2["_circuit_opens"] == 3


def T(name, cond, detail=None):
    results.append((name, bool(cond), detail))
    return bool(cond)


saved_pool = m._pool
try:
    # 供 record_* 按 pid 查找
    a = new_st("fast", rem=2000, ttfb=2.0)
    b = new_st("slow", rem=200, ttfb=25.0, streak=5, stalls=3)
    c = new_st("avg", rem=2000)
    d = new_st("brk", rem=500)
    e = new_st("can", rem=500)
    f = new_st("win", rem=2000)
    m._pool = {"fast": a, "slow": b, "avg": c, "brk": d, "can": e, "win": f, "l3": None}

    # ── 打分与排序 ──
    sa, sb = m._account_health_score(a), m._account_health_score(b)
    T("健康分区间(0~100)", 0 <= sa <= 100 and 0 <= sb <= 100, (sa, sb))
    T("健康路由: 快号 > 慢号", sa > sb, (sa, sb))

    for x in (2, 3, 28, 29):   # 均值 15.5s → 中低分
        c["_ttfbs"].append(x)
    sc = m._account_health_score(c)
    f2 = new_st("f2", rem=2000, ttfb=2.0)
    T("TTFB 均值评分生效(慢样本拉低)", sc < m._account_health_score(f2), (sc, m._account_health_score(f2)))

    # ── 失败链 → 熔断(阶梯) ──
    m._account_record_failure("brk", "error")
    m._account_record_failure("brk", "error")
    T("错误链未达阈值不熔断", m._circuit_label(d) == "closed" and d["_err_streak"] == 2)
    m._account_record_failure("brk", "stall")
    T("第3次失败打开熔断(30s)", m._circuit_label(d) == "open" and d["_circuit_opens"] == 1)
    rem1 = d["_circuit_until"] - time.time()
    T("一级阶梯≈30s", 25 < rem1 <= 31, round(rem1, 1))
    T("熔断拦截期内被排除", m._circuit_is_open(d))

    # 到期后半开 → 再失败升级 120s
    d["_circuit_until"] = time.time() - 1
    T("到期后半开(可探针)", m._circuit_label(d) == "half_open" and not m._circuit_is_open(d))
    m._account_record_failure("brk", "error")
    rem2 = d["_circuit_until"] - time.time()
    T("半开探针失败 → 升级 120s", d["_circuit_opens"] == 2 and 90 < rem2 <= 121, round(rem2, 1))
    T("三级阶梯上限 600s", ladder3_ok())

    # ── 成功复位 + 关闭熔断 ──
    d["_circuit_until"] = time.time() - 1
    m._account_record_success("brk", ttfb=1.8)
    T("成功关闭熔断并清错误链", m._circuit_label(d) == "closed" and d["_err_streak"] == 0
      and d["_circuit_opens"] == 0 and d["_last_ttfb"] == 1.8)

    # ── cancel 不算故障 ──
    m._account_record_failure("can", "cancel")
    T("客户端断连(cancel)不计故障", e["_err_streak"] == 0 and m._circuit_label(e) == "closed")

    # ── 滚动窗口 10 样本封顶 ──
    for i in range(15):
        m._account_record_success("win", ttfb=float(i))
    T("TTFB 窗口封顶 10 样本", len(f["_ttfbs"]) == 10, len(f["_ttfbs"]))
finally:
    m._pool = saved_pool

for name, passed, detail in results:
    print(f"[{'PASS' if passed else 'FAIL'}] {name}" + (f"  detail={detail}" if detail is not None else ""))
    ok = ok and passed
print("=" * 40)
print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
