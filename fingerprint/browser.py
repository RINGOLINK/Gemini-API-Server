"""指纹浏览器模块入口 —— Schema v2 启动管线"""
import asyncio, json, shutil
from pathlib import Path
from patchright.async_api import async_playwright
from .fingerprint_gen import generate, save as save_fp, load as load_fp, inject_scripts
from .pool import current_version

STORAGE_ROOT = Path(__file__).parent / "storage"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
MANAGE_PORT = 4446


class FingerprintBrowser:
    def __init__(self, account_id: str, proxy: dict | None = None,
                 os_type: str = "windows", config: dict | None = None):
        self.account_id = account_id
        self._profile_dir = STORAGE_ROOT / account_id
        self._browser = None
        self._context = None
        self._playwright = None
        self._pproxy = None  # 认证 socks5 的本地 pproxy 转发子进程(成熟库,高并发容错)

        # Load profile.json v2
        meta_file = self._profile_dir / "profile.json"
        self._config = config or {}
        if meta_file.exists():
            self._config = json.loads(meta_file.read_text(encoding="utf-8"))

        # Override from args
        if proxy: self._config.setdefault("proxy", proxy)
        if os_type:
            self._config.setdefault("os", {"family": os_type, "version": "win10"})

    @property
    def fingerprint(self) -> dict:
        fp = load_fp(self._profile_dir)
        if not fp:
            fp = generate(self.account_id, "windows")
            save_fp(fp, self._profile_dir)
        return fp

    async def launch(self, headless: bool = True):
        self._playwright = await async_playwright().start()
        fp = self.fingerprint
        cfg = self._config
        basic = cfg.get("basic", {})
        geoloc = cfg.get("geolocation", {})
        lang = cfg.get("language", {})
        hw = cfg.get("hardware", {})

        # follow_ip 需要出口 IP 的国家码：launch 时按当前代理配置查询一次
        if lang.get("follow_ip") and not cfg.get("ip_info"):
            try:
                from .netprobe import resolve_proxy_config, query_ip
                ptype, phost, pport, puser, ppass = resolve_proxy_config(cfg)
                ipi = query_ip(ptype, phost, pport, puser, ppass)
                if ipi:
                    cfg["ip_info"] = ipi
            except Exception as e:
                print(f"[browser] follow_ip 出口 IP 查询失败: {e}")
        proxy_cfg = cfg.get("proxy", {})

        # Build proxy arg
        proxy_arg = None
        proxy_mode = (proxy_cfg or {}).get("mode", "direct")
        if proxy_mode == "system":
            # 跟随系统代理：从注册表读取真实地址
            from .netprobe import get_system_proxy
            sys_proxy = get_system_proxy()
            if sys_proxy:
                proxy_arg = {"server": sys_proxy}
        elif proxy_mode.startswith("bound:"):
            # 绑定代理：从代理池取信息。open 层已做故障转移，这里取最终选定的代理。
            from . import proxy_manager as _pm
            _px = _pm.get_proxy(proxy_mode[6:])
            if _px:
                proxy_arg = {"server": f"{_px['scheme']}://{_px['host']}:{_px['port']}"}
                if _px.get("username"):
                    proxy_arg["username"] = _px["username"]
                    proxy_arg["password"] = _px.get("password", "")
                proxy_mode = _px["scheme"]  # 供后续 direct/quic 判断用
            else:
                proxy_mode = "direct"  # 代理已删，回退直连
        elif proxy_mode not in ("direct", "", None):
            host = (proxy_cfg.get("host") or "").strip()
            port = proxy_cfg.get("port") or 1080
            user = proxy_cfg.get("username") or ""
            pw = proxy_cfg.get("password") or ""
            if host:
                if user:
                    if proxy_mode in ("socks5", "socks5h", "socks"):
                        # Chromium 命令行不支持 socks5 带认证 → 用本地转发器代做认证(已修复 CONNECT LEN bug)
                        try:
                            from . import local_socks
                            import os as _ose
                            _maxc = int(_ose.environ.get("PROXY_MAX_CONCURRENT", "64"))
                            lp = local_socks.start_local_socks(host, int(port), user, pw,
                                                               max_concurrent=_maxc)
                            proxy_arg = {"server": f"socks5://127.0.0.1:{lp}"}
                        except Exception:
                            proxy_arg = {"server": f"{proxy_mode}://{host}:{port}"}
                    else:
                        # http/https 认证 Chromium 支持 URL 内嵌
                        proxy_arg = {"server": f"{proxy_mode}://{user}:{pw}@{host}:{port}"}
                else:
                    proxy_arg = {"server": f"{proxy_mode}://{host}:{port}"}

        # Build args
        _disable_feats = []
        # 加载内置 2FA 自动填充扩展(每窗口独立加载;临时目录复制防 Chromium 扩展缓存)
        import os as _os, tempfile as _tf
        _ext_src = Path(__file__).parent / "ext2fa"
        _ext_arg = ""
        if _ext_src.exists():
            _ext_tmp = _os.path.join(self._profile_dir, ".ext-" + _os.urandom(4).hex())
            _os.makedirs(_ext_tmp, exist_ok=True)
            _ext_arg = _os.path.join(_ext_tmp, "2fa")
            shutil.copytree(str(_ext_src), _ext_arg)
        args = []
        if _ext_arg:
            args += [f"--disable-extensions-except={_ext_arg}", f"--load-extension={_ext_arg}"]
        args += [
            f"--window-size={fp['viewport']['width']},{fp['viewport']['height']}",
            "--disable-blink-features=AutomationControlled",
            # 管理/服务端口放行(本机 4444-4446,防御性保留)
            "--explicitly-allowed-ports=4444,4445,4446",
            # 抹除自动化痕迹
            "--disable-infobars",
            "--no-first-run",
            "--no-default-browser-check",
            # 关闭会暴露自动化的后台特性
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-ipc-flooding-protection",
        ]
        # Hardware accel：不显式传 --enable-gpu（正常浏览器默认开，显式传反而异常）
        if not basic.get("hw_accel", True):
            args.append("--disable-gpu")
        # 直连模式：显式禁用任何代理（含系统代理）
        # Chromium 在 Windows 上默认继承 WinINET 系统代理，
        # 仅 --no-proxy-server 不足以覆盖，需同时禁用自动探测并清空 PAC。
        if proxy_mode in ("direct", "", None):
            args.extend([
                "--no-proxy-server",
                "--proxy-server=direct://",
                "--proxy-bypass-list=*",
                "--winhttp-proxy-resolver",
                "--no-pac-url",
            ])
        # HTTPS errors
        if cfg.get("ignore_https_errors"):
            args.append("--ignore-certificate-errors")
        # WebRTC
        webrtc = cfg.get("webrtc", "private")
        # 注意：--webrtc-ip-handling-policy 是不存在的无效 flag，已删除。
        # 有效 flag 仅 --force-webrtc-ip-handling-policy，取值须为 RFC8828 下划线字符串。
        # 修正 typo：disable_non_proxied-udp(连字符,无效→回退default泄露) → disable_non_proxied_udp(下划线)
        if webrtc == "replace":
            # 只暴露代理出口，禁止 host 候选（否则本机 IP 仍会通过 STUN 泄露）
            args.append("--force-webrtc-ip-handling-policy=default_public_interface_only")
        elif webrtc == "private":
            args.extend([
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                "--enforce-webrtc-ip-permission-check",
            ])
        elif webrtc == "disabled":
            _disable_feats += ["WebRtcHideLocalIpsWithMdns", "WebRTC"]
            # 彻底禁用 WebRTC（RTCPeerConnection），无任何候选泄露
            args.append("--disable-blink-features=RTCPeerConnection")
        # QUIC/HTTP3 走 UDP 443 无法经 HTTP/SOCKS 代理，禁用防真实 IP 经 QUIC 直连泄露
        args.append("--disable-quic")
        # 合并所有 disable-features（重复传参后者覆盖前者，必须一次传入）
        # Prerender2：禁用预渲染，根治「点NTP推荐快捷方式换target导致注入失效」
        _disable_feats += ["MediaRouter", "Prerender2", "Prerender2FallbackToNoStatePrefetch"]
        args.append("--disable-features=" + ",".join(dict.fromkeys(_disable_feats)))

        # Kernel version: use specific chromium build if configured
        kernel_ver = cfg.get('kernel_version', '')
        executable_path = None
        if kernel_ver:
            from .kernel_manager import get_executable
            executable_path = get_executable(kernel_ver)

        # 时区/语言：follow_ip 时按出口 IP 推导，避免「IP在日本时区却是上海」
        tz = lang.get("timezone") or "Asia/Shanghai"
        loc = lang.get("locale") or "zh-CN"
        if lang.get("follow_ip"):
            ipi = cfg.get("ip_info") or {}
            cc = (ipi.get("country_code") or "").upper()
            mapping = {
                "JP": ("Asia/Tokyo", "ja-JP"),   "KR": ("Asia/Seoul", "ko-KR"),
                "CN": ("Asia/Shanghai", "zh-CN"), "TW": ("Asia/Taipei", "zh-TW"),
                "HK": ("Asia/Hong_Kong", "zh-HK"), "SG": ("Asia/Singapore", "en-SG"),
                "US": ("America/New_York", "en-US"), "GB": ("Europe/London", "en-GB"),
                "DE": ("Europe/Berlin", "de-DE"), "FR": ("Europe/Paris", "fr-FR"),
                "RU": ("Europe/Moscow", "ru-RU"), "IN": ("Asia/Kolkata", "en-IN"),
                "BR": ("America/Sao_Paulo", "pt-BR"), "AU": ("Australia/Sydney", "en-AU"),
                "CA": ("America/Toronto", "en-CA"), "NL": ("Europe/Amsterdam", "nl-NL"),
                "VN": ("Asia/Ho_Chi_Minh", "vi-VN"), "TH": ("Asia/Bangkok", "th-TH"),
                "ID": ("Asia/Jakarta", "id-ID"), "MY": ("Asia/Kuala_Lumpur", "ms-MY"),
            }
            if cc in mapping:
                tz, loc = mapping[cc]

        # no_viewport=True：页面视口跟随窗口大小（可拖拽自适应），
        # 指纹的 screen/innerWidth 仍由注入脚本伪装，不受影响。
        # headless 不支持 no_viewport，需保留固定 viewport。
        kwargs = dict(
            user_data_dir=str(self._profile_dir / "userdata"),
            headless=headless,
            viewport=(fp["viewport"] if headless else None),
            no_viewport=(not headless),
            user_agent=cfg.get("ua") or fp["ua"],
            timezone_id=tz,
            locale=loc,
            # 不传 downloads_path：Playwright 会把下载劫持成 GUID artifact（文件名不可用）。
            # 改用下方 CDP Browser.setDownloadBehavior，原始文件名落 downloads/{pid}/。
            ignore_https_errors=cfg.get("ignore_https_errors", False),
            args=args,
        )
        if proxy_arg:
            kwargs["proxy"] = proxy_arg
            # 关键:本地地址(欢迎页 127.0.0.1/localhost/::1)必须绕过代理,否则连欢迎页都要走代理→超时。
            # Chromium 的 <-loopback> 有时不生效(127.0.0.1 仍走代理),这里显式列出本机地址。
            args.append("--proxy-bypass-list=localhost;127.0.0.1;::1")
            # 注意:不再加 --host-resolver-rules=MAP * ~NOTFOUND。
            # 该规则会把所有域名映射为"不可解析",导致 Chromium 无法处理 gemini.google.com 等域名
            # → 不走代理 → ERR_TIMED_OUT。Chromium 的 socks5 代理默认就把域名交给代理解析,
            # 本身已防 DNS 泄露,无需 host-resolver-rules。

        # Geolocation
        if geoloc.get("mode") in ("allow", "ask"):
            kwargs["permissions"] = ["geolocation"]
            if geoloc.get("lat"):
                kwargs["geolocation"] = {
                    "latitude": geoloc["lat"],
                    "longitude": geoloc["lon"],
                    "accuracy": geoloc.get("accuracy", 100),
                }

        # Clear cache before launch
        if basic.get("clear_cache_on_start"):
            ud = self._profile_dir / "userdata" / "Default"
            for sub in ["Cache", "Code Cache", "GPUCache", "DawnWebGPUCache"]:
                d = ud / sub
                if d.exists(): shutil.rmtree(d, ignore_errors=True)

        if executable_path:
            kwargs['executable_path'] = executable_path
        self._context = await self._playwright.chromium.launch_persistent_context(**kwargs)

        # 下载目录接管（仅用户手动点击的下载）：原始文件名落「浏览器下载根/{pid}/」。
        # Agent 的 download_file 走 sw fetch→后端落盘（downloads/{pid}/），与此分离。
        # 每次启动强制设置，覆盖 Chromium 默认行为；不影响 Playwright download 事件。
        try:
            _sf = STORAGE_ROOT / "settings.json"
            _broot = str(Path.home() / "Downloads" / "Gemini-API Download")
            if _sf.exists():
                try:
                    _broot = json.loads(_sf.read_text(encoding="utf-8")).get("browser_download_root") or _broot
                except Exception:
                    pass
            dl_dir = Path(_broot) / self.account_id
            dl_dir.mkdir(parents=True, exist_ok=True)
            _pg = self._context.pages[0] if self._context.pages else await self._context.new_page()
            _ds = await self._context.new_cdp_session(_pg)
            await _ds.send("Browser.setDownloadBehavior", {
                "behavior": "allow",
                "downloadPath": str(dl_dir),
                "eventsEnabled": True,
            })
            print(f"[browser] 下载目录已接管: {dl_dir}")
        except Exception as e:
            print(f"[browser] setDownloadBehavior failed: {e}")

        # Inject fingerprint scripts
        inject_cfg = dict(cfg)
        inject_cfg["seed"] = cfg.get("seed") or self.account_id
        inject_cfg["viewport"] = fp["viewport"]
        inject_cfg["language"] = dict(inject_cfg.get("language") or {})
        inject_cfg["language"]["locale"] = loc
        self._init_script = inject_scripts(inject_cfg)
        self._injected_pages = set()

        # 指纹伪装：context 级 add_init_script —— 框架对 context 内所有
        # page/popup/iframe 自动注册 addScriptToEvaluateOnNewDocument（主世界），
        # 比逐页 CDP 注入更可靠（含用户手动开的标签）。
        try:
            await self._context.add_init_script(self._init_script)
        except Exception as e:
            print(f"[browser] context add_init_script failed: {e}")

        # 自动填充仍需 page 级 CDP（按真实 URL 匹配账号），保留逐页注入
        def _on_page(p):
            asyncio.get_event_loop().create_task(self._inject_page(p))
        self._context.on("page", _on_page)

        for _p in self._context.pages:
            await self._inject_page(_p)

        # DNT header
        await self._context.set_extra_http_headers({"DNT": "1"})

        # Port scan protection
        async def _block_local(route):
            url = route.request.url
            try:
                # 放行管理面板端口(欢迎页等), 其余本地地址仍拦截
                if url.startswith((f"http://127.0.0.1:{MANAGE_PORT}", f"http://localhost:{MANAGE_PORT}")):
                    await route.continue_()
                elif url.startswith(("http://127.0.0.", "http://localhost",
                                     "https://127.0.0.", "https://localhost")):
                    await route.abort()
                else:
                    await route.continue_()
            except Exception:
                pass
        await self._context.route("**/*", _block_local)

        # 1) 打开欢迎页（复用/新建第一个标签，替换初始 about:blank）
        welcome_url = f"http://127.0.0.1:{MANAGE_PORT}/welcome?pid=" + str(self.account_id)
        wpage = None
        try:
            # 启动时 persistent_context 自带一个 about:blank 页, 直接复用它加载欢迎页(不留空白标签)
            existing = self._context.pages
            if existing:
                wpage = existing[0]
                await wpage.goto(welcome_url, wait_until="domcontentloaded", timeout=15000)
            else:
                wpage = await self.new_page()
                await wpage.goto(welcome_url, wait_until="domcontentloaded", timeout=15000)
        except Exception as e:
            print(f"[browser] welcome page failed: {e}")

        # 2) 欢迎页先置前，startup_urls 随后并发静默加载（减少标签逐个跳动）
        if wpage is not None:
            try:
                await wpage.bring_to_front()
            except Exception:
                pass
        startup_urls = basic.get("startup_urls") or []
        if not isinstance(startup_urls, list):
            startup_urls = str(startup_urls).splitlines()

        async def _open_startup(url):
            url = (url or "").strip()
            if not url:
                return
            if not url.startswith(("http://", "https://", "about:", "file://")):
                url = "https://" + url
            try:
                page = await self.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                print(f"[browser] startup_url failed: {url} -> {e}")

        _tasks = [_open_startup(u) for u in startup_urls if (u or "").strip()]
        if _tasks:
            await asyncio.gather(*_tasks, return_exceptions=True)

        # 3) 兜底：若欢迎页失败且无任何页面，补一个空白页
        if not self._context.pages:
            await self.new_page()
        # 4) 全部加载完后，欢迎页固定显示在最前
        if wpage is not None:
            try:
                await wpage.bring_to_front()
            except Exception:
                pass

        return self

    @property
    def context(self): return self._context

    def _autofill_script(self, account: dict | None) -> str:
        """登录页自动填充脚本。账号由后端预取嵌入（不跨域 fetch）。

        account: {username, password} 或 None（无匹配则不注入逻辑）。
        加固：识别 type=text 伪装的密码框、用户名/密码分别填充、轮询覆盖分步渲染。
        """
        if not account:
            return "(function(){/* no account */})();"
        user = account.get("username", "")
        pwd = account.get("password", "")
        return """
(function(){
  if(window.__omAutofill)return; window.__omAutofill=true;
  var USER=%s, PWD=%s;
  var userDone=false, pwdDone=false;

  function isVisible(el){
    if(!el)return false;
    var r=el.getBoundingClientRect();
    var st=getComputedStyle(el);
    return r.width>0&&r.height>0&&st.visibility!=='hidden'&&st.display!=='none'&&!el.disabled;
  }
  // 找密码框：type=password，或 type=text 但 autocomplete/name/id/placeholder 暗示密码
  function findPwd(){
    var p=document.querySelector('input[type="password"]');
    if(p&&isVisible(p))return p;
    var cands=document.querySelectorAll('input[type="text"],input:not([type]),input[type="tel"]');
    for(var i=0;i<cands.length;i++){var el=cands[i];
      var hint=((el.autocomplete||'')+' '+(el.name||'')+' '+(el.id||'')+' '+(el.placeholder||'')+' '+(el.getAttribute('aria-label')||'')).toLowerCase();
      if(/current-password|new-password|password|passwd|pwd|密码/.test(hint)&&isVisible(el))return el;
    }
    return null;
  }
  function findUser(pwdEl){
    var scope=(pwdEl&&pwdEl.closest('form'))||document;
    var cands=scope.querySelectorAll('input[type="text"],input[type="email"],input[type="tel"],input:not([type])');
    var first=null;
    for(var i=0;i<cands.length;i++){var el=cands[i];
      if(el===pwdEl||!isVisible(el))continue;
      var hint=((el.name||'')+' '+(el.id||'')+' '+(el.placeholder||'')+' '+(el.autocomplete||'')+' '+(el.getAttribute('aria-label')||'')).toLowerCase();
      if(/current-password|new-password|password|passwd|pwd|密码/.test(hint))continue;  // 跳过密码框
      if(!first)first=el;
      if(/user|email|phone|account|login|name|username|账号|用户名|手机|邮箱/.test(hint))return el;
    }
    return first;
  }
  function setVal(el,val){
    if(!el)return false;
    try{
      el.focus();
      var proto=el instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
      var setter=Object.getOwnPropertyDescriptor(proto,'value');
      if(setter&&setter.set)setter.set.call(el,val); else el.value=val;
      el.dispatchEvent(new Event('input',{bubbles:true}));
      el.dispatchEvent(new Event('change',{bubbles:true}));
      el.dispatchEvent(new KeyboardEvent('keydown',{bubbles:true}));
      el.dispatchEvent(new KeyboardEvent('keyup',{bubbles:true}));
      el.blur();
      return true;
    }catch(e){return false}
  }
  function showTip(){
    try{
      if(document.getElementById('__omAutofillTip'))return;
      var tip=document.createElement('div');
      tip.id='__omAutofillTip';
      tip.textContent='已自动填充账号：'+USER;
      tip.style.cssText='position:fixed;top:12px;right:12px;background:rgba(34,197,94,.95);color:#fff;padding:8px 14px;border-radius:8px;font-size:13px;z-index:999999;box-shadow:0 2px 12px rgba(0,0,0,.3)';
      document.body.appendChild(tip);
      setTimeout(function(){tip.style.opacity='0';tip.style.transition='opacity .4s';setTimeout(function(){tip.remove()},400)},3000);
    }catch(e){}
  }
  function tryFill(){
    var pwdEl=findPwd();
    // 密码框优先（只有密码框存在才认为是登录页）
    if(pwdEl&&!pwdDone){
      if(setVal(pwdEl,PWD))pwdDone=true;
    }
    if(!userDone){
      var userEl=findUser(pwdEl);
      if(userEl&&setVal(userEl,USER))userDone=true;
    }
    if(pwdDone&&userDone)showTip();
  }
  var n=0;
  var t=setInterval(function(){
    n++;
    tryFill();
    if((pwdDone&&userDone)||n>30){clearInterval(t)}
  },500);
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',tryFill);
  else tryFill();
  // MutationObserver 应对延迟插入的登录框
  try{
    var mo=new MutationObserver(function(){if(!pwdDone||!userDone)tryFill()});
    mo.observe(document.documentElement,{childList:true,subtree:true});
    setTimeout(function(){mo.disconnect()},15000);
  }catch(e){}
})();
""" % (repr(user), repr(pwd))

    async def _page_account(self, page) -> dict | None:
        """根据页面当前 URL 匹配账号（后端预取，含明文密码）。"""
        try:
            from . import account_manager as am
            url = page.url or ""
            if not url or url.startswith(("about:", "data:", "chrome:")):
                return None
            return am.find_account_for_url(self.account_id, url)
        except Exception:
            return None

    async def _inject_page(self, page):
        """page 级 CDP 注入主世界脚本（去重）。必须先 Page.enable。"""
        if not self._context or not getattr(self, "_init_script", None):
            return
        key = id(page)
        if key in self._injected_pages:
            return
        try:
            s = await self._context.new_cdp_session(page)
            await s.send("Page.enable")
            # 指纹脚本已由 context.add_init_script 全局注册，此处不再重复
            # 自动填充：按"当前/每次导航后的真实 URL"注入（含匹配账号）
            async def _fill_now():
                try:
                    acc = await self._page_account(page)
                    if not acc:
                        return
                    af = self._autofill_script(acc)
                    await s.send("Runtime.evaluate", {"expression": af, "awaitPromise": False})
                except Exception:
                    pass
            await _fill_now()  # 当前页立即尝试
            def _on_nav(frame):
                if frame == page.main_frame:
                    asyncio.get_event_loop().create_task(_fill_now())
            page.on("framenavigated", _on_nav)
            self._injected_pages.add(key)
        except Exception as e:
            print(f"[browser] main-world inject failed: {e}")

    async def new_page(self):
        page = await self._context.new_page()
        await self._inject_page(page)
        return page

    async def close(self):
        try:
            if self._context: await self._context.close()
        except: pass
        try:
            if self._playwright: await self._playwright.stop()
        except: pass
        # 终止本地 pproxy 转发子进程
        if getattr(self, "_pproxy", None):
            try:
                self._pproxy.terminate()
                self._pproxy = None
            except Exception:
                pass

    @property
    def download_dir(self) -> Path:
        # 与 server.py 的 DOWNLOADS_ROOT 统一：PROJECT_ROOT/downloads/{pid}/
        return PROJECT_ROOT / "downloads" / self.account_id

    async def export_cookies(self, path: str | None = None) -> list[dict]:
        if not path:
            path = str(self._profile_dir / "cookies.json")
        cookies = await self._context.cookies()
        Path(path).write_text(json.dumps(cookies, indent=2))
        return cookies

    async def clear_cookies(self):
        await self._context.clear_cookies()

    def info(self) -> dict:
        fp = self.fingerprint
        cfg = self._config
        return {
            "account_id": self.account_id,
            "seed": cfg.get("seed", self.account_id),
            "chromium_version": current_version(),
            "fingerprint": fp,
            "profile_dir": str(self._profile_dir),
            "proxy": cfg.get("proxy", {}).get("mode", "direct"),
        }
