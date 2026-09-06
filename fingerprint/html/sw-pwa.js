/* OpenMedia PWA 最小 Service Worker——仅为满足安装条件，不缓存不拦截 */
self.addEventListener("install", function(){ self.skipWaiting(); });
self.addEventListener("activate", function(e){ e.waitUntil(clients.claim()); });
self.addEventListener("fetch", function(){});
