// 站内播放弹窗（检索页、数据看板共用）。
// 用法：给可点击元素加上 playAttrs(url, title, platform, localFile, imageUrl) 生成的 data-* 属性，点击即在本页弹窗播放。
// 依赖页面的 CSS 变量 --card --line --muted --accent；接口：/api/play、/api/relay、/api/jobs/{id}
(function () {
  const css = `
  .thumb .play { position:absolute; inset:0; margin:auto; width:46px; height:46px; border-radius:50%;
                 background:rgba(0,0,0,.55); color:#fff; display:flex; align-items:center; justify-content:center;
                 font-size:18px; opacity:.85; pointer-events:none; }
  .thumb:hover .play { opacity:1; background:var(--accent); }
  .pmodal { position:fixed; inset:0; background:rgba(0,0,0,.78); z-index:50; display:flex;
            align-items:center; justify-content:center; padding:16px; }
  .pmodal.off { display:none; }
  .pmodal .mbox { background:var(--card); border-radius:12px; width:min(1000px,100%); max-height:100%;
                  display:flex; flex-direction:column; overflow:hidden; }
  .pmodal .mhead { display:flex; gap:10px; align-items:center; padding:10px 14px; border-bottom:1px solid var(--line); }
  .pmodal .mhead .t { flex:1; font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .pmodal button { border-radius:8px; padding:5px 10px; font-size:12px; cursor:pointer;
                   background:transparent; color:var(--accent); border:1px solid var(--accent); }
  .pmodal button.solid { background:var(--accent); color:#fff; }
  .pmodal .stage { background:#000; aspect-ratio:16/9; width:100%; display:flex; align-items:center; justify-content:center;
                   color:#ccc; max-height:calc(100vh - 140px); }
  .pmodal .stage.tall { aspect-ratio:auto; height:min(78vh,760px); }
  .pmodal .stage iframe, .pmodal .stage video, .pmodal .stage img { width:100%; height:100%; border:0; object-fit:contain; }
  .pmodal .mfoot { display:flex; flex-wrap:wrap; gap:8px 16px; align-items:center; padding:10px 14px; font-size:12px; color:var(--muted); }
  .pmodal .mfoot .sp { flex:1; } .pmodal .mfoot a { color:var(--accent); }`;
  const style = document.createElement("style");
  style.textContent = css;
  document.head.appendChild(style);

  const box = document.createElement("div");
  box.className = "pmodal off";
  box.innerHTML = `<div class="mbox">
    <div class="mhead"><span class="t"></span>
      <button data-act="prev" title="上一条（←）">‹ 上一条</button>
      <button data-act="next" title="下一条（→）">下一条 ›</button>
      <button data-act="close" class="solid" title="关闭（Esc）">关闭</button></div>
    <div class="stage"></div>
    <div class="mfoot"><span class="msg"></span><span class="sp"></span>
      <button data-act="relay" style="display:none">服务器中转播放</button>
      <a class="orig" href="#" target="_blank" rel="noopener">原页面打开 ↗</a></div>
  </div>`;
  const mount = () => document.body.appendChild(box);
  document.body ? mount() : document.addEventListener("DOMContentLoaded", mount);

  const q = s => box.querySelector(s);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const safe = u => /^https?:\/\//i.test(u || "") || /^\/[^/]/.test(u || "") ? u : "#";
  let list = [], idx = -1, token = 0, curUrl = "";

  window.playAttrs = function (url, title, platform, local = "", image = "") {
    return `data-play="${esc(url)}" data-title="${esc(title || "")}" data-pf="${esc(platform || "")}"`
         + (local ? ` data-local="${esc(local)}"` : "") + (image ? ` data-image="${esc(image)}"` : "");
  };

  function stage(html, tall) {
    q(".stage").className = "stage" + (tall ? " tall" : "");
    q(".stage").innerHTML = html;
  }
  function msg(t) { q(".msg").textContent = t; }
  function relayBtn(show) { q('[data-act="relay"]').style.display = show ? "" : "none"; }
  function playVideo(src) { stage(`<video src="${esc(safe(src))}" controls autoplay playsinline></video>`); }

  async function open(d) {
    const my = ++token;
    curUrl = d.play;
    box.classList.remove("off");
    q(".t").textContent = (d.pf ? `[${d.pf}] ` : "") + d.title;
    q(".orig").href = safe(d.play);
    relayBtn(false);
    msg("");
    if (d.image) return stage(`<img src="${esc(safe(d.image))}" referrerpolicy="no-referrer" alt="">`);
    if (d.local) { msg("播放的是已下载到服务器的文件"); return playVideo(d.local); }
    stage("正在解析播放地址…");
    try {
      const r = await (await fetch(`/api/play?url=${encodeURIComponent(d.play)}`)).json();
      if (my !== token) return;
      if (r.kind === "iframe") {
        stage(`<iframe src="${esc(r.src)}" allow="autoplay; fullscreen; encrypted-media; picture-in-picture" allowfullscreen></iframe>`,
              /douyin/.test(r.src));
        relayBtn(true);
        msg("官方播放器内嵌。打不开（如 YouTube 需翻墙）可点“服务器中转播放”");
      } else if (r.kind === "video") playVideo(r.src);
      else relay(d.play);
    } catch (e) { stage("解析失败，请点“原页面打开”"); }
  }

  async function relay(url) {
    const my = ++token;
    relayBtn(false);
    stage("该站不能内嵌，服务器正在拉取视频（通常 10~60 秒）…");
    try {
      const r = await fetch("/api/relay", {method: "POST", headers: {"Content-Type": "application/json"},
                                           body: JSON.stringify({url})});
      const jid = (await r.json()).job_id;
      while (my === token) {
        const j = await (await fetch(`/api/jobs/${jid}`)).json();
        if (j.status === "queued" || j.status === "running") { await new Promise(r => setTimeout(r, 1500)); continue; }
        if (my !== token) return;
        if (j.status === "done" && j.result && j.result.ok) { msg("服务器中转播放（480p 缓存）"); return playVideo(j.result.src); }
        return stage(`<div style="padding:20px;text-align:center">无法站内播放：${esc((j.result && j.result.error) || j.error || "未知错误")}<br><br>请点下方“原页面打开”</div>`);
      }
    } catch (e) { stage("中转失败，请点“原页面打开”"); }
  }

  function close() { token++; box.classList.add("off"); q(".stage").innerHTML = ""; }
  function step(n) {
    if (!list.length) return;
    idx = (idx + n + list.length) % list.length;
    open(list[idx].dataset);
  }

  box.addEventListener("click", e => {
    if (e.target === box) return close();
    const act = e.target.closest("[data-act]")?.dataset.act;
    if (act === "close") close();
    else if (act === "prev") step(-1);
    else if (act === "next") step(1);
    else if (act === "relay") relay(curUrl);
  });
  document.addEventListener("click", e => {
    const a = e.target.closest("[data-play]");
    if (!a || e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) return;
    e.preventDefault();
    list = [...document.querySelectorAll("[data-play]")].filter((x, i, arr) =>
      arr.findIndex(y => y.dataset.play === x.dataset.play) === i);
    idx = list.findIndex(x => x.dataset.play === a.dataset.play);
    open(a.dataset);
  });
  document.addEventListener("keydown", e => {
    if (box.classList.contains("off")) return;
    if (e.key === "Escape") close();
    else if (e.key === "ArrowLeft") step(-1);
    else if (e.key === "ArrowRight") step(1);
  });
})();
