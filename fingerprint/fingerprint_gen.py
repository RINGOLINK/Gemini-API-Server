"""
指纹生成与注入 —— 指纹隔离层的核心
每账号一个固定 seed → 同账号指纹稳定、跨账号指纹不同
"""
import json
import random
import hashlib
from pathlib import Path
from typing import Any

# 真实 UA 池（按操作系统分类）
UA_POOL = {
    "windows": [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    ],
    "mac": [
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    ],
}

RESOLUTIONS = [
    (1920, 1080), (2560, 1440), (1366, 768), (1440, 900), (1536, 864),
]

TIMEZONES = {
    "US": "America/New_York", "UK": "Europe/London",
    "CN": "Asia/Shanghai", "JP": "Asia/Tokyo", "KR": "Asia/Seoul",
}

LOCALES = {
    "US": "en-US", "UK": "en-GB", "CN": "zh-CN", "JP": "ja-JP", "KR": "ko-KR",
}

GPU_VENDORS = [
    "Google Inc. (NVIDIA)", "Google Inc. (AMD)", "Google Inc. (Intel)",
]

GPU_RENDERERS = [
    "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11)",
    "ANGLE (AMD, AMD Radeon RX 7800 XT Direct3D11)",
    "ANGLE (Intel, Intel(R) UHD Graphics Direct3D11)",
]


def _derive(seed: str, pool: list, key: str = "") -> Any:
    """从 seed 派生一个稳定的选择"""
    h = hashlib.md5(f"{seed}:{key}".encode()).hexdigest()
    idx = int(h, 16) % len(pool)
    return pool[idx]


def generate(seed: str, os_type: str = "windows") -> dict:
    """生成一套完整指纹，同一 seed 每次调用结果相同"""
    rng = random.Random(seed)
    res = rng.choice(RESOLUTIONS)
    return {
        "seed": seed,
        "ua": _derive(seed, UA_POOL.get(os_type, UA_POOL["windows"]), "ua"),
        "viewport": {"width": res[0], "height": res[1]},
        "timezone": _derive(seed, list(TIMEZONES.keys()), "tz"),
        "locale": _derive(seed, list(TIMEZONES.keys()), "tz"),
        "gpu_vendor": _derive(seed, GPU_VENDORS, "gpuv"),
        "gpu_renderer": _derive(seed, GPU_RENDERERS, "gpur"),
        "hardware_concurrency": rng.choice([4, 8, 12, 16]),
        "device_memory": rng.choice([4, 8, 16]),
        "canvas_noise": rng.random(),  # 固定噪声种子
        "webgl_noise": rng.random(),
    }


def save(fp: dict, storage_dir: Path):
    storage_dir.mkdir(parents=True, exist_ok=True)
    (storage_dir / "fingerprint.json").write_text(json.dumps(fp, indent=2, ensure_ascii=False))


def load(storage_dir: Path) -> dict | None:
    f = storage_dir / "fingerprint.json"
    if f.exists():
        return json.loads(f.read_text())
    return None


def inject_scripts(cfg: dict | None = None) -> str:
    """返回注入页面的 JS：确定性 Canvas/Audio 噪声 + 完整反自动化检测。

    关键设计：所有噪声都由 seed 派生，保证「同一 profile 多次访问结果一致」，
    否则检测站会因为两次读数不同而直接判定 Canvas 被篡改。
    """
    cfg = cfg or {}
    webgl = cfg.get("webgl") or {}
    hw = cfg.get("hardware") or {}
    lang = cfg.get("language") or {}
    os_cfg = cfg.get("os") or {}
    seed = str(cfg.get("seed") or cfg.get("id") or "default")

    fam = (os_cfg.get("family") or "windows").lower()

    # platform 必须与 UA 一致（检测站先比 UA 和 platform）。
    # UA 是第一锚点：若 UA 与 os.family 冲突，以 UA 为准反推 fam。
    ua_str = (cfg.get("ua") or "").lower()
    if "mac os x" in ua_str or "macintosh" in ua_str:
        fam = "macos"
    elif "windows" in ua_str or "win64" in ua_str or "wow64" in ua_str:
        fam = "windows"
    elif "linux" in ua_str and "android" not in ua_str:
        fam = "linux"

    platform = {"windows": "Win32", "macos": "MacIntel", "linux": "Linux x86_64"}.get(fam, "Win32")

    # UA 平台与 GPU 渲染器必须自洽：Linux/macOS 不可能出现 Direct3D11
    vendor = webgl.get("vendor") or ""
    renderer = webgl.get("renderer") or ""

    # 空值兜底：不配置 WebGL 时给该 OS 的安全默认，绝不泄露真实 GPU
    if not vendor or not renderer:
        defaults = {
            "windows": ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
            "macos":   ("Apple Inc.", "Apple M2"),
            "linux":   ("Google Inc. (Intel)", "ANGLE (Intel, Mesa Intel(R) UHD Graphics 630, OpenGL 4.6)"),
        }
        vendor, renderer = defaults.get(fam, defaults["windows"])
    # 已配置但与 fam 矛盾时纠正（Linux/macOS 不可能出现 Direct3D11）
    elif fam != "windows" and "Direct3D" in renderer:
        if fam == "macos":
            vendor, renderer = "Apple Inc.", "Apple M2"
        else:
            vendor = "Google Inc. (Intel)"
            renderer = "ANGLE (Intel, Mesa Intel(R) UHD Graphics 630, OpenGL 4.6)"

    # screen 取标准物理分辨率（>= viewport），避免「screen == viewport == 窗口」的篡改信号。
    # 选不小于 viewport 的最常见物理分辨率。
    vp = cfg.get("viewport") or {}
    vp_w = vp.get("width") or 1920
    vp_h = vp.get("height") or 1080
    common_res = [(1366, 768), (1536, 864), (1920, 1080), (2560, 1440), (3440, 1440), (3840, 2160)]
    scr_w, scr_h = 1920, 1080
    for w, h in common_res:
        if w >= vp_w and h >= vp_h:
            scr_w, scr_h = w, h
            break
    else:
        scr_w, scr_h = max(vp_w, 1920), max(vp_h, 1080)

    payload = json.dumps({
        "seed": seed,
        "vendor": vendor,
        "renderer": renderer,
        "cores": hw.get("cpu_cores") or 8,
        "memory": hw.get("memory_gb") or 16,
        "locale": lang.get("locale") or "",
        "platform": platform,
        "fam": fam,
        "scrW": scr_w,
        "scrH": scr_h,
    }, ensure_ascii=False)

    return r"""
(() => {
  const CFG = __CFG__;

  // ===== 确定性伪随机（seed -> 稳定序列）=====
  let _h = 2166136261;
  for (let i = 0; i < CFG.seed.length; i++) {
    _h ^= CFG.seed.charCodeAt(i);
    _h = Math.imul(_h, 16777619);
  }
  const mkRng = (salt) => {
    let s = (_h ^ Math.imul(salt + 1, 2654435761)) >>> 0;
    return () => {
      s ^= s << 13; s >>>= 0;
      s ^= s >>> 17;
      s ^= s << 5;  s >>>= 0;
      return s / 4294967296;
    };
  };

  // ===== 原生 toString 防护：让被 patch 的函数看起来仍是原生 =====
  const _natives = new WeakMap();
  const origFnToString = Function.prototype.toString;
  Function.prototype.toString = function () {
    if (_natives.has(this)) return 'function ' + _natives.get(this) + '() { [native code] }';
    return origFnToString.call(this);
  };
  _natives.set(Function.prototype.toString, 'toString');
  const mark = (fn, name) => { _natives.set(fn, name); return fn; };

  // 属性劫持：优先改写 prototype 上的 getter（实例层定义会被运行时重置）
  const define = (obj, prop, value) => {
    const getter = mark(() => value, 'get ' + prop);
    let done = false;
    try {
      const proto = Object.getPrototypeOf(obj);
      if (proto && Object.getOwnPropertyDescriptor(proto, prop)) {
        Object.defineProperty(proto, prop, { get: getter, enumerable: true, configurable: true });
        done = true;
      }
    } catch (e) {}
    if (!done) {
      try {
        Object.defineProperty(obj, prop, { get: getter, enumerable: true, configurable: true });
      } catch (e) {}
    }
  };

  // ===== 1. 机器人检测：webdriver 保持原生，勿 delete/勿 defineProperty =====
  // 真实 Chrome(≥M88) 中 Navigator.prototype.webdriver 描述符本就存在，
  // native getter 返回 false（patchright 已用 --disable-blink-features=AutomationControlled
  // 关闭自动化特征，原生即 false）。delete 会造成「原型无描述符」的抹除痕迹，
  // defineProperty 会造成「非 native getter」痕迹，都是 WebDriver Advance 的判据。
  // 正确做法：完全不碰，保留原生描述符。
  // （兜底：仅当原生值异常为 true 时才干预——正常不会发生）
  try {
    if (navigator.webdriver === true) {
      Object.defineProperty(Object.getPrototypeOf(navigator), 'webdriver',
        { get: mark(() => false, 'get webdriver'), configurable: true });
    }
  } catch (e) {}

  // CDP / 自动化残留变量
  for (const k of ['cdc_adoQpoasnfa76pfcZLmcfl_Array', 'cdc_adoQpoasnfa76pfcZLmcfl_Promise',
                   'cdc_adoQpoasnfa76pfcZLmcfl_Symbol', '__webdriver_evaluate',
                   '__selenium_evaluate', '__webdriver_script_fn', '__driver_evaluate',
                   '__fxdriver_evaluate', '__webdriver_unwrapped', '_Selenium_IDE_Recorder']) {
    try { delete window[k]; delete document[k]; } catch (e) {}
  }

  // chrome.runtime：真实 Chrome 必有，headless 常缺
  if (!window.chrome) window.chrome = {};
  if (!window.chrome.runtime) {
    window.chrome.runtime = {
      connect: mark(function () { return { onDisconnect: { addListener: mark(() => {}, 'addListener') } }; }, 'connect'),
      sendMessage: mark(function () {}, 'sendMessage'),
      id: undefined,
    };
  }
  if (!window.chrome.csi) window.chrome.csi = mark(() => ({}), 'csi');
  if (!window.chrome.loadTimes) window.chrome.loadTimes = mark(() => ({}), 'loadTimes');
  if (!window.chrome.app) window.chrome.app = { isInstalled: false };

  // Notification.permission：自动化环境常默认 denied，新用户真实状态应为 default。
  // denied + 全新 profile = 矛盾（没人点过拒绝），是 WebDriver Advance 的典型判据。
  try {
    Object.defineProperty(Notification, 'permission', {
      get: mark(() => 'default', 'permission_get'),
      configurable: true,
    });
  } catch (e) {}

  // permissions.query：headless 下 notifications 常返回异常组合
  try {
    const origQuery = navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query = mark(function (p) {
      if (p && p.name === 'notifications') {
        return Promise.resolve({ state: 'default', onchange: null });
      }
      return origQuery(p);
    }, 'query');
  } catch (e) {}

  // plugins / mimeTypes：真实浏览器非空
  try {
    if (!navigator.plugins || navigator.plugins.length === 0) {
      const mk = (name, desc, type) => ({ name, description: desc, filename: 'internal-pdf-viewer',
                                          length: 1, 0: { type, suffixes: 'pdf', description: desc } });
      const arr = [
        mk('PDF Viewer', 'Portable Document Format', 'application/pdf'),
        mk('Chrome PDF Viewer', 'Portable Document Format', 'application/pdf'),
        mk('Chromium PDF Viewer', 'Portable Document Format', 'application/pdf'),
        mk('Microsoft Edge PDF Viewer', 'Portable Document Format', 'application/pdf'),
        mk('WebKit built-in PDF', 'Portable Document Format', 'application/pdf'),
      ];
      arr.forEach((p, i) => { arr[p.name] = p; });
      define(navigator, 'plugins', arr);
    }
  } catch (e) {}

  // ===== 2. Canvas：确定性噪声（同输入 -> 同输出）=====
  const canvasNoise = (ctx, w, h, tag) => {
    try {
      const img = ctx.getImageData(0, 0, w, h);
      const rng = mkRng(tag);
      const d = img.data;
      // 固定步长扫描，扰动幅度 ±1，人眼不可见但指纹稳定
      for (let i = 0; i < d.length; i += 4 * 97) {
        const delta = rng() < 0.5 ? -1 : 1;
        d[i]     = Math.max(0, Math.min(255, d[i] + delta));
        d[i + 1] = Math.max(0, Math.min(255, d[i + 1] + delta));
        d[i + 2] = Math.max(0, Math.min(255, d[i + 2] + delta));
      }
      ctx.putImageData(img, 0, 0);
    } catch (e) {}
  };

  const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
  HTMLCanvasElement.prototype.toDataURL = mark(function (...args) {
    try {
      if (this.width > 1 && this.height > 1) {
        const ctx = this.getContext('2d');
        if (ctx) canvasNoise(ctx, this.width, this.height, 1);
      }
    } catch (e) {}
    return origToDataURL.apply(this, args);
  }, 'toDataURL');

  const origToBlob = HTMLCanvasElement.prototype.toBlob;
  if (origToBlob) {
    HTMLCanvasElement.prototype.toBlob = mark(function (...args) {
      try {
        if (this.width > 1 && this.height > 1) {
          const ctx = this.getContext('2d');
          if (ctx) canvasNoise(ctx, this.width, this.height, 1);
        }
      } catch (e) {}
      return origToBlob.apply(this, args);
    }, 'toBlob');
  }

  const origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
  CanvasRenderingContext2D.prototype.getImageData = mark(function (...args) {
    const res = origGetImageData.apply(this, args);
    try {
      const rng = mkRng(2);
      const d = res.data;
      for (let i = 0; i < d.length; i += 4 * 97) {
        const delta = rng() < 0.5 ? -1 : 1;
        d[i] = Math.max(0, Math.min(255, d[i] + delta));
      }
    } catch (e) {}
    return res;
  }, 'getImageData');

  // ===== 3. WebGL：getParameter + getExtension 一致伪装 =====
  const patchGL = (proto, isGL2) => {
    if (!proto) return;
    const origGetParam = proto.getParameter;
    proto.getParameter = mark(function (p) {
      // UNMASKED_VENDOR_WEBGL / UNMASKED_RENDERER_WEBGL
      if (CFG.vendor && p === 37445) return CFG.vendor;
      if (CFG.renderer && p === 37446) return CFG.renderer;
      // VENDOR / RENDERER
      if (CFG.vendor && p === 7936) return CFG.vendor;
      if (CFG.renderer && p === 7937) return CFG.renderer;
      return origGetParam.apply(this, arguments);
    }, 'getParameter');

    // 保证 debug 扩展存在，否则「查不到 UNMASKED」本身就是异常信号
    const origGetExt = proto.getExtension;
    proto.getExtension = mark(function (name) {
      const r = origGetExt.apply(this, arguments);
      if (name === 'WEBGL_debug_renderer_info' && !r) {
        return { UNMASKED_VENDOR_WEBGL: 37445, UNMASKED_RENDERER_WEBGL: 37446 };
      }
      return r;
    }, 'getExtension');

    const origSupported = proto.getSupportedExtensions;
    proto.getSupportedExtensions = mark(function () {
      const list = origSupported.apply(this, arguments) || [];
      if (!list.includes('WEBGL_debug_renderer_info')) list.push('WEBGL_debug_renderer_info');
      return list;
    }, 'getSupportedExtensions');

    // readPixels 噪声：防止逐像素比对
    const origRead = proto.readPixels;
    if (origRead) {
      proto.readPixels = mark(function (x, y, w, h, fmt, type, pixels) {
        const r = origRead.apply(this, arguments);
        try {
          if (pixels && pixels.length > 32) {
            const rng = mkRng(3);
            for (let i = 0; i < pixels.length; i += 4 * 199) {
              pixels[i] = Math.max(0, Math.min(255, pixels[i] + (rng() < 0.5 ? -1 : 1)));
            }
          }
        } catch (e) {}
        return r;
      }, 'readPixels');
    }
  };
  patchGL(window.WebGLRenderingContext && WebGLRenderingContext.prototype, false);
  patchGL(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype, true);

  // ===== 3b. WebGPU vendor 与 WebGL 对齐（同一物理 GPU 厂商必须一致）=====
  // 从 WebGL vendor/renderer 提取厂商，映射 WebGPU 简短名
  const _gpuVendor = (() => {
    const s = ((CFG.vendor || '') + ' ' + (CFG.renderer || '')).toLowerCase();
    if (s.includes('nvidia') || s.includes('geforce') || s.includes('rtx') || s.includes('gtx')) return 'nvidia';
    if (s.includes('amd') || s.includes('radeon')) return 'amd';
    if (s.includes('apple') || s.includes('m1') || s.includes('m2') || s.includes('m3')) return 'apple';
    if (s.includes('intel') || s.includes('iris') || s.includes('uhd')) return 'intel';
    if (s.includes('qualcomm') || s.includes('adreno')) return 'qualcomm';
    if (s.includes('arm') || s.includes('mali')) return 'arm';
    return 'intel';
  })();
  try {
    if (navigator.gpu && navigator.gpu.requestAdapter) {
      const origRA = navigator.gpu.requestAdapter.bind(navigator.gpu);
      navigator.gpu.requestAdapter = mark(async function (...args) {
        const adapter = await origRA(...args);
        if (!adapter) return adapter;
        // 新版: adapter.info (GPUAdapterInfo 只读对象)
        try {
          if (adapter.info) {
            const info = {};
            for (const k of ['vendor', 'architecture', 'device', 'description']) {
              try { info[k] = adapter.info[k]; } catch (e) { info[k] = ''; }
            }
            info.vendor = _gpuVendor;
            Object.defineProperty(adapter, 'info', { get: mark(() => info, 'get info'), configurable: true });
          }
        } catch (e) {}
        // 旧版: adapter.requestAdapterInfo()
        try {
          if (adapter.requestAdapterInfo) {
            const origInfo = adapter.requestAdapterInfo.bind(adapter);
            adapter.requestAdapterInfo = mark(async function (...a) {
              const r = await origInfo(...a);
              try { r.vendor = _gpuVendor; } catch (e) {}
              return r;
            }, 'requestAdapterInfo');
          }
        } catch (e) {}
        return adapter;
      }, 'requestAdapter');
    }
  } catch (e) {}

  // ===== 4. AudioContext 确定性噪声 =====
  try {
    const AP = window.AnalyserNode && AnalyserNode.prototype;
    if (AP) {
      const origFloat = AP.getFloatFrequencyData;
      AP.getFloatFrequencyData = mark(function (arr) {
        origFloat.apply(this, arguments);
        try {
          const rng = mkRng(4);
          for (let i = 0; i < arr.length; i += 37) arr[i] += (rng() - 0.5) * 0.0002;
        } catch (e) {}
      }, 'getFloatFrequencyData');
    }
    const CB = window.AudioBuffer && AudioBuffer.prototype;
    if (CB) {
      const origCh = CB.getChannelData;
      CB.getChannelData = mark(function (ch) {
        const d = origCh.apply(this, arguments);
        try {
          const rng = mkRng(5);
          for (let i = 0; i < d.length; i += 1009) d[i] += (rng() - 0.5) * 1e-7;
        } catch (e) {}
        return d;
      }, 'getChannelData');
    }
  } catch (e) {}

  // ===== 5. 硬件 / 语言 / 屏幕一致性 =====
  if (CFG.cores)    define(navigator, 'hardwareConcurrency', CFG.cores);
  if (CFG.memory)   define(navigator, 'deviceMemory', CFG.memory);
  if (CFG.platform) define(navigator, 'platform', CFG.platform);
  if (CFG.locale) {
    define(navigator, 'language', CFG.locale);
    define(navigator, 'languages', Object.freeze([CFG.locale, CFG.locale.split('-')[0]]));
  }
  // 屏幕尺寸与 viewport 对齐，消除「窗口小屏幕大」的矛盾
  try {
    define(screen, 'width', CFG.scrW);
    define(screen, 'height', CFG.scrH);
    define(screen, 'availWidth', CFG.scrW);
    define(screen, 'availHeight', CFG.scrH - 40);
    define(screen, 'colorDepth', 24);
    define(screen, 'pixelDepth', 24);
  } catch (e) {}

  // macOS/Linux 不应报告 touch 支持异常
  if (CFG.fam !== 'windows') {
    try { define(navigator, 'maxTouchPoints', 0); } catch (e) {}
  }

  // ===== 6. Worker 环境伪装（关键：检测站从 Worker 读真实 cores/mem）=====
  // Worker 是独立 JS 全局，page 级注入进不去。patch Worker 构造，
  // 在其源码前 prepend 一段自包含的 navigator 伪装。
  const WORKER_SPOOF = `
    (function(){
      const C = { cores: ${CFG.cores}, mem: ${CFG.memory}, platform: '${CFG.platform}', locale: '${CFG.locale}' };
      const def = (o,p,v)=>{ try{
        const proto = Object.getPrototypeOf(o);
        if (proto && Object.getOwnPropertyDescriptor(proto,p)) {
          Object.defineProperty(proto,p,{get:()=>v,enumerable:true,configurable:true});
        } else { Object.defineProperty(o,p,{get:()=>v,enumerable:true,configurable:true}); }
      }catch(e){} };
      if (typeof navigator !== 'undefined') {
        if (C.cores) def(navigator,'hardwareConcurrency',C.cores);
        if (C.mem)   def(navigator,'deviceMemory',C.mem);
        if (C.platform) def(navigator,'platform',C.platform);
        if (C.locale) { def(navigator,'language',C.locale); def(navigator,'languages',Object.freeze([C.locale, C.locale.split('-')[0]])); }
      }
    })();
  `;

  const patchWorkerCtor = (Orig, name) => {
    if (!Orig) return Orig;
    const Patched = function (source, opts) {
      try {
        // 仅处理可注入的形式：字符串 URL / Blob / 代码字符串
        const buildBlob = (code) => new Blob([WORKER_SPOOF + '\n' + code], { type: 'application/javascript' });
        if (source instanceof Blob) {
          const url = URL.createObjectURL(source);
          let code = '';
          try {
            const xhr = new XMLHttpRequest();
            xhr.open('GET', url, false);
            xhr.send();
            if (xhr.status >= 200 && xhr.status < 300) code = xhr.responseText;
          } catch (e) {}
          URL.revokeObjectURL(url);
          return new Orig(URL.createObjectURL(buildBlob(code)), opts);
        }
        if (typeof source === 'string') {
          // 远程/相对 URL：先拉取源码再 prepend（同源限制下尽力而为）
          try {
            const xhr = new XMLHttpRequest();
            xhr.open('GET', source, false); // 同步，Worker 创建本就阻塞
            xhr.send();
            if (xhr.status >= 200 && xhr.status < 300) {
              return new Orig(URL.createObjectURL(buildBlob(xhr.responseText)), opts);
            }
          } catch (e) { /* 跨源则放弃注入，回退原样 */ }
          return new Orig(source, opts);
        }
        return new Orig(source, opts);
      } catch (e) {
        return new Orig(source, opts);
      }
    };
    // 保留原型与静态属性，避免 instanceof / new.target 检测露馅
    Patched.prototype = Orig.prototype;
    try { Object.setPrototypeOf(Patched, Orig); } catch (e) {}
    try { Object.defineProperty(Patched, 'name', { value: name }); } catch (e) {}
    mark(Patched, name);
    return Patched;
  };


  try { window.Worker = patchWorkerCtor(window.Worker, 'Worker'); } catch (e) {}
  try { if (window.SharedWorker) window.SharedWorker = patchWorkerCtor(window.SharedWorker, 'SharedWorker'); } catch (e) {}
})();
""".replace("__CFG__", payload)


def randomize_v2(pid: str):
    """一键随机化：以 os.family 为锚点，派生 UA/WebGL/硬件，保证全链路自洽。

    任何被检测站交叉比对的字段都从同一个 os.family 派生，杜绝
    「UA=Mac 但 platform=Win32」「macOS 配 Direct3D」这类矛盾。
    """
    import json, random
    from pathlib import Path
    prof_path = Path(__file__).parent / "storage" / pid / "profile.json"
    if not prof_path.exists(): return
    data = json.loads(prof_path.read_text(encoding="utf-8"))

    seed = data.get("seed", pid)
    rng = random.Random(seed + "|" + __import__("datetime").datetime.now().strftime("%Y%m%d%H%M%S"))

    # ---- 1. 锚点：先定 OS 家族 ----
    family = rng.choice(["windows", "windows", "windows", "macos", "linux"])  # windows 加权

    # ---- 2. OS 版本与 family 匹配 ----
    os_versions = {
        "windows": ["win10", "win11"],
        "macos":   ["14_5", "14_4", "13_6"],
        "linux":   ["ubuntu22", "ubuntu24"],
    }
    os_ver = rng.choice(os_versions[family])
    data["os"] = {"family": family, "version": os_ver}

    # ---- 3. UA：从该 OS 派生，并对齐内核版本的 Chrome 号 ----
    kernel_ver = data.get("kernel_version", "")
    if kernel_ver:
        from media_core.fingerprint.kernel_manager import get_ua_for_kernel
        data["ua"] = get_ua_for_kernel(kernel_ver, family)
    else:
        ua_key = {"windows": "windows", "macos": "mac", "linux": "linux"}[family]
        pool = UA_POOL.get(ua_key) or UA_POOL["windows"]
        data["ua"] = rng.choice(pool)

    # ---- 4. WebGL：从该 OS 专属池取（与图形栈自洽）----
    webgl_pool = {
        "windows": [
            ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
            ("Google Inc. (AMD)",    "ANGLE (AMD, AMD Radeon RX 7800 XT Direct3D11 vs_5_0 ps_5_0, D3D11)"),
            ("Google Inc. (Intel)",  "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ],
        "macos": [
            ("Apple Inc.", "Apple M2"),
            ("Apple Inc.", "Apple M1"),
            ("Apple Inc.", "Apple M3"),
            ("Apple Inc.", "ANGLE Metal Renderer: Apple M2, Unspecified Version"),
        ],
        "linux": [
            ("Google Inc. (Intel)",  "ANGLE (Intel, Mesa Intel(R) UHD Graphics 630, OpenGL 4.6)"),
            ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650, OpenGL 4.6)"),
            ("Mesa",                 "Mesa Intel(R) UHD Graphics 630 (CFL GT2)"),
        ],
    }
    vendor, renderer = rng.choice(webgl_pool[family])
    data["webgl"] = {"vendor": vendor, "renderer": renderer}

    # ---- 5. 设备信息 ----
    data["device"] = {
        "name": f"DESKTOP-{seed[:6].upper()}",
        "host_ip": f"192.168.{rng.randint(1,255)}.{rng.randint(1,255)}",
        "mac": "%02x:%02x:%02x:%02x:%02x:%02x" % tuple(rng.randint(0, 255) for _ in range(6)),
    }

    # ---- 6. 硬件：常见消费级配置 ----
    data["hardware"] = {
        "cpu_cores": rng.choice([4, 6, 8, 12, 16, 24]),
        "memory_gb": rng.choice([8, 16, 16, 32]),
    }

    data["updated_at"] = __import__("datetime").datetime.now().isoformat()
    tmp = prof_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(prof_path)

