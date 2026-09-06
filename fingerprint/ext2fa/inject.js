// 2FA 密钥写入页:由窗口进程(playwright)打开 chrome-extension://<id>/inject.html?platform=&account=&secret=&note= 完成写入
const p = new URLSearchParams(location.search);
const secret = p.get("secret") || "";
const account = p.get("account") || "";
const platform = (p.get("platform") || "google").toLowerCase();
const note = p.get("note") || "";
const st = document.getElementById("st");

if (!secret) {
  st.textContent = "EMPTY";
} else {
  try {
    chrome.storage.local.get("keys", (d) => {
      let keys = d.keys || [];
      keys = keys.filter(k => !(k.platform === platform && k.account === account));
      keys.push({ platform, account, secret, note });
      chrome.storage.local.set({ keys }, () => {
        st.textContent = "SAVED:" + keys.length;
      });
    });
  } catch (e) {
    st.textContent = "ERR:" + e.message;
  }
}
