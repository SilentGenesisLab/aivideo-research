// 统一左侧菜单：放在 <body> 开头同步执行，避免页面闪动。数据看板的子页面用 #hash 切换，不刷新页面。
// 先显示上次的数据（存在 sessionStorage），再后台取最新的，变化了才重新渲染：切换菜单时内容立即出现。
window.swrJSON = async function (url, render) {
  const key = "swr:" + url;
  let old = null;
  try { old = sessionStorage.getItem(key); } catch (e) {}
  if (old) { try { render(JSON.parse(old), true); } catch (e) { old = null; } }
  const r = await fetch(url, {cache: "no-store"});
  if (!r.ok) throw new Error(r.status);
  const text = await r.text();
  if (text !== old) {
    try { sessionStorage.setItem(key, text); } catch (e) {}
    render(JSON.parse(text), false);
  }
};
// 登录过期：页面里的接口请求返回 401 时回到登录页
(function () {
  const raw = window.fetch;
  window.fetch = async function (...args) {
    const r = await raw.apply(this, args);
    const url = String(args[0] && args[0].url || args[0] || "");
    if (r.status === 401 && !/\/api\/v1\//.test(url) && !/\/api\/login/.test(url))
      location.href = "/login?expired=1&next=" + encodeURIComponent(location.pathname + location.search + location.hash);
    return r;
  };
})();
// 外站封面、产品图统一走服务器缩略图缓存
window.thumbUrl = (u, w) => /^https?:\/\//i.test(u || "") ? `/img?w=${w || 480}&u=${encodeURIComponent(u)}` : (u || "");

(function () {
  const I = {
    search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
    lib: '<rect x="3" y="4" width="18" height="14" rx="2"/><path d="m10 8.5 4 2.5-4 2.5z"/><path d="M7 21h10"/>',
    chart: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    list: '<path d="M8 6h13M8 12h13M8 18h13"/><circle cx="3.5" cy="6" r="1"/><circle cx="3.5" cy="12" r="1"/><circle cx="3.5" cy="18" r="1"/>',
    api: '<path d="M8 9 4 12l4 3M16 9l4 3-4 3M13.5 6l-3 12"/>',
    book: '<path d="M4 5a2 2 0 0 1 2-2h13v16H6a2 2 0 0 0-2 2z"/><path d="M4 19V5"/>',
    doc: '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6M8 13h8M8 17h5"/>',
    play: '<circle cx="12" cy="12" r="9"/><path d="m10 8.5 5 3.5-5 3.5z"/>',
  };
  const MENU = [
    ["工作台"],
    ["search", "/", "", "视频检索", I.search],
    ["videos", "/dashboard", "videos", "视频库", I.lib],
    ["overview", "/dashboard", "overview", "数据看板", I.chart],
    ["tasks", "/dashboard", "tasks", "任务日志", I.list],
    ["api", "/dashboard", "api", "接口调用", I.api],
    ["帮助"],
    ["method", "/method", "", "检索方法说明", I.book],
    ["developer", "/developer", "", "接口文档", I.doc, true],
    ["swagger", "/api/v1/docs", "", "在线调试", I.play, true],
  ];
  const logo = "/favicon.svg";
  const items = MENU.map(m => m.length === 1 ? `<div class="grp">${m[0]}</div>`
    : `<a class="item" data-key="${m[0]}" href="${m[1]}${m[2] ? "#" + m[2] : ""}"${m[5] ? ' target="_blank"' : ""}>
         <svg viewBox="0 0 24 24">${m[4]}</svg><span>${m[3]}</span>${m[5] ? '<span class="ext">↗</span>' : ""}</a>`).join("");
  const side = document.createElement("aside");
  side.className = "side";
  side.innerHTML = `<a class="brand" href="/"><img src="${logo}" alt=""><span><b>参考片检索</b><small>产品图 → 参考广告片</small></span></a>
    ${items}<div class="foot"><div class="me"><span class="av">A</span><span class="nm">admin</span>
      <a href="/logout" class="out" title="退出登录">退出</a></div></div>`;
  const top = document.createElement("div");
  top.className = "sidebar-top";
  top.innerHTML = `<button aria-label="菜单">☰</button><img src="${logo}" alt=""><b>参考片检索</b>`;
  const mask = document.createElement("div");
  mask.className = "side-mask";
  document.body.classList.add("has-side");
  document.body.prepend(mask);
  document.body.prepend(side);
  document.body.prepend(top);

  const toggle = open => { side.classList.toggle("open", open); mask.classList.toggle("open", open); };
  top.querySelector("button").onclick = () => toggle(!side.classList.contains("open"));
  mask.onclick = () => toggle(false);
  side.addEventListener("click", e => { if (e.target.closest("a.item")) toggle(false); });

  function mark() {
    const path = location.pathname, hash = location.hash.slice(1);
    let key = path === "/" ? "search" : path === "/method" ? "method" : "";
    if (path === "/dashboard") key = ["videos", "tasks", "api"].includes(hash) ? hash : "overview";
    side.querySelectorAll("a.item").forEach(a => a.classList.toggle("on", a.dataset.key === key));
  }
  mark();
  window.addEventListener("hashchange", mark);

  // 任务日志旁显示进行中的任务数
  async function badge() {
    try {
      const list = await (await fetch("/api/tasks", {cache: "no-store"})).json();
      const n = list.filter(t => t.status === "queued" || t.status === "running").length;
      const a = side.querySelector('[data-key="tasks"]');
      a.querySelector(".cnt")?.remove();
      if (n) a.insertAdjacentHTML("beforeend", `<span class="cnt" title="进行中/排队中">${n}</span>`);
      setTimeout(badge, n ? 5000 : 20000);
    } catch (e) { setTimeout(badge, 30000); }
  }
  badge();
  fetch("/api/me").then(r => r.json()).then(j => {
    side.querySelector(".me .nm").textContent = j.user;
    side.querySelector(".me .av").textContent = (j.user || "A").slice(0, 1).toUpperCase();
  }).catch(() => {});
})();
