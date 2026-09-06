// Gemini 2FA Autofill —— content script
// 检测 Google 登录 2FA 输入框,从扩展 storage 读取该窗口的 TOTP 密钥,自动生成并填入 6 位验证码

// ---- TOTP 工具(RFC 6238,HMAC-SHA1,30s 窗口)----
const B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";

function b32decode(s) {
  s = String(s || "").toUpperCase().replace(/[\s=]/g, "");
  let bits = 0, val = 0, out = [];
  for (let i = 0; i < s.length; i++) {
    const c = B32.indexOf(s[i]);
    if (c < 0) continue;
    val = (val << 5) | c;
    bits += 5;
    if (bits >= 8) {
      out.push((val >>> (bits - 8)) & 0xff);
      bits -= 8;
    }
  }
  return new Uint8Array(out);
}

function otpauthSecret(secret) {
  // 支持直接 base32,或 otpauth://totp/...?secret=XXX
  let s = String(secret || "");
  const m = s.match(/[?&]secret=([^&]+)/);
  if (m) s = decodeURIComponent(m[1]);
  return s;
}

async function hmacSha1(keyBytes, msgBytes) {
  const key = await crypto.subtle.importKey(
    "raw", keyBytes, { name: "HMAC", hash: "SHA-1" }, false, ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", key, msgBytes);
  return new Uint8Array(sig);
}

function counterBytes(counter) {
  const buf = new Uint8Array(8);
  for (let i = 7; i >= 0; i--) { buf[i] = counter & 0xff; counter = Math.floor(counter / 256); }
  return buf;
}

async function genTotp(secret, step = 30, digits = 6) {
  const s = otpauthSecret(secret);
  if (!s) return null;
  const counter = Math.floor(Date.now() / 1000 / step);
  const h = await hmacSha1(b32decode(s), counterBytes(counter));
  const off = h[h.length - 1] & 0x0f;
  const code = (((h[off] & 0x7f) << 24) | ((h[off + 1] & 0xff) << 16) |
                ((h[off + 2] & 0xff) << 8) | (h[off + 3] & 0xff)) % Math.pow(10, digits);
  return String(code).padStart(digits, "0");
}

// ---- 查找 Google 2FA 输入框 ----
function findTotpInput() {
  const sels = [
    'input[name="TotpPin"]',
    'input#totpPin',
    'input[autocomplete="one-time-code"]',
    'input[inputmode="numeric"][name*="totp" i]',
    'input[name*="otp" i]',
    'input[id*="totp" i]',
    'input[placeholder*="验证码" i]',
    'input[placeholder*="code" i]',
    'input[placeholder*="6位" i]'
  ];
  for (const sel of sels) {
    const el = document.querySelector(sel);
    if (el && el.offsetParent !== null) return el;
  }
  return null;
}

function setNativeValue(el, value) {
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
  setter.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
}

let lastFilled = 0;

async function tryFill() {
  const input = findTotpInput();
  if (!input) return;
  const keys = (await chrome.storage.local.get("keys")).keys || [];
  // 取该窗口 platform=google 的密钥(窗口隔离,一般一个 Google 账号,取第一个)
  const k = keys.find(x => (x.platform || "").toLowerCase() === "google") || keys[0];
  if (!k || !k.secret) return;
  const code = await genTotp(k.secret);
  if (!code) return;
  // 避免重复填充同一个码
  if (Date.now() - lastFilled < 25000) return;
  setNativeValue(input, code);
  lastFilled = Date.now();
  // 触发提交:Google 通常填完自动继续;保险起见触发 Enter
  setTimeout(() => {
    try {
      input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", bubbles: true }));
      input.dispatchEvent(new KeyboardEvent("keyup", { key: "Enter", code: "Enter", bubbles: true }));
    } catch (e) {}
  }, 600);
}

function boot() {
  tryFill();
  // 动态页面(分步渲染)轮询
  const mo = new MutationObserver(() => { tryFill(); });
  mo.observe(document.documentElement, { childList: true, subtree: true });
  setTimeout(() => mo.disconnect(), 120000);
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
else boot();
