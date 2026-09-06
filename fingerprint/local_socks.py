# -*- coding: utf-8 -*-
"""本地 SOCKS5 认证转发器。

Chromium 命令行 --proxy-server 不支持带认证的 socks5,而多数代理需要认证。
本模块起一个本地 socks5(127.0.0.1:临时端口,无认证),Chromium 连它;转发器代为向上游
(带认证 socks5)握手 + 认证,再双向转发。用法:
    port = start_local_socks(upstream_host, upstream_port, username, password)
浏览器 --proxy-server=socks5://127.0.0.1:<port>
"""
import socket
import threading

_BUF = 65536


def _recvn(s: socket.socket, n: int) -> bytes:
    b = b""
    while len(b) < n:
        c = s.recv(n - len(b))
        if not c:
            raise EOFError
        b += c
    return b


def _upstream_hello(up: socket.socket, user: str, pw: str) -> None:
    """向上游 socks5 发起握手(带认证)并完成认证。"""
    up.sendall(b"\x05\x01\x02")  # 提供 username/password 认证
    ver, sel = _recvn(up, 2)
    if sel == 0x02:
        u = user.encode("utf-8", "ignore")
        p = pw.encode("utf-8", "ignore")
        up.sendall(bytes([0x01, len(u)]) + u + bytes([len(p)]) + p)
        _ver, status = _recvn(up, 2)
        if status != 0x00:
            raise Exception("上游认证失败")
    elif sel != 0x00:
        raise Exception("上游无可用认证方式")


def _read_connect_req(conn: socket.socket) -> bytes:
    """读完整 CONNECT 请求(本地客户端发来),原样返回以便转发到上游。"""
    head = _recvn(conn, 4)  # VER CMD RSV ATYP
    atyp = head[3]
    if atyp == 0x01:  # IPv4
        rest = _recvn(conn, 4 + 2)
    elif atyp == 0x03:  # domain — 注意 LEN 字节也要保留进请求
        ln_byte = _recvn(conn, 1)
        ln = ln_byte[0]
        rest = ln_byte + _recvn(conn, ln + 2)  # LEN + 域名 + 端口
    elif atyp == 0x04:  # IPv6
        rest = _recvn(conn, 16 + 2)
    else:  # IPv6
        rest = _recvn(conn, 16 + 2)
    return head + rest


def _pump(a: socket.socket, b: socket.socket) -> None:
    def _copy(src, dst):
        try:
            while True:
                data = src.recv(_BUF)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    t1 = threading.Thread(target=_copy, args=(a, b), daemon=True)
    t2 = threading.Thread(target=_copy, args=(b, a), daemon=True)
    t1.start()
    t2.start()


def _handle(conn: socket.socket, up_host, up_port, up_user, up_pass, sem):
    import time as _t
    _t0 = _t.time()
    def _dbg(m):
        try:
            print(f"[ls] {m}", flush=True)
        except Exception:
            pass
    _dbg(f"enter up={up_host}:{up_port} from={conn.getpeername()}")
    if sem:
        sem.acquire()
    try:
        # 本地握手(Chromium 无认证)
        gv, nm = _recvn(conn, 2)
        for _ in range(nm):
            _recvn(conn, 1)
        conn.sendall(b"\x05\x00")  # 同意无认证
        _dbg("greet ok")
        # 读 CONNECT 请求(只读一次,重试时复用)
        req = _read_connect_req(conn)
        _dbg(f"got connect req len={len(req)} hex={req.hex()}")

        up = None
        last = None
        for _attempt in range(3):
            try:
                up = socket.create_connection((up_host, up_port), timeout=20)
                _dbg("upstream tcp connect ok")
                _upstream_hello(up, up_user, up_pass)
                _dbg("upstream auth ok")
                up.sendall(req)
                _dbg("sent connect req to upstream")
                _resp = _recvn(up, 10)
                _dbg(f"upstream connect resp rep={_resp[1]}")
                conn.sendall(_resp)
                break
            except Exception as e:
                last = e
                _dbg(f"attempt fail {type(e).__name__}: {str(e)[:60]}")
                try:
                    if up:
                        up.close()
                except OSError:
                    pass
                try:
                    _t.sleep(0.3)
                except Exception:
                    pass
                up = None

        if up is None:
            raise last if last else RuntimeError("上游连接失败")

        try:
            print(f"[local_socks] {up_host}:{up_port} 认证+CONNECT 成功 "
                  f"{_t.time()-_t0:.1f}s rep={_resp[1]}", flush=True)
        except Exception:
            pass
        # 双向转发
        _pump(conn, up)
    except Exception as e:
        try:
            print(f"[local_socks] {up_host}:{up_port} 失败: {type(e).__name__}: {e}", flush=True)
        except Exception:
            pass
        try:
            conn.close()
        except OSError:
            pass
    finally:
        if sem:
            sem.release()


def start_local_socks(up_host: str, up_port: int, up_user: str, up_pass: str,
                      listen="127.0.0.1", listen_port: int = 0,
                      max_concurrent: int = 4) -> int:
    """启动本地转发器,返回监听端口。用信号量限制同时连上游的连接数。
    Chromium 一开页几十并发把上游(低并发代理)打爆 → 限流排队。max_concurrent 可调。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((listen, listen_port))
    srv.listen(128)
    port = srv.getsockname()[1]
    sem = threading.BoundedSemaphore(max_concurrent) if max_concurrent and max_concurrent > 0 else None

    def _accept():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                break
            threading.Thread(target=_handle, args=(conn, up_host, up_port, up_user, up_pass, sem), daemon=True).start()

    threading.Thread(target=_accept, daemon=True).start()
    return port
