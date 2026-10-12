// 最小接入示例（Node.js 18+，无第三方依赖）：用图片地址提交检索 → 等待结果 → 打印参考视频。
//   REFSEARCH_URL=http://<服务地址>:8770 REFSEARCH_KEY=sk_live_xxx \
//   node examples/node_client.mjs https://your-cdn.com/product.jpg "30秒TVC，年轻白领"
const BASE = (process.env.REFSEARCH_URL || "http://127.0.0.1:8770").replace(/\/$/, "") + "/api/v1";
const HEADERS = { "X-API-Key": process.env.REFSEARCH_KEY, "Content-Type": "application/json" };

async function api(method, path, body) {
  const r = await fetch(BASE + path, { method, headers: HEADERS, body: body ? JSON.stringify(body) : undefined });
  const data = await r.json();
  if (!r.ok) throw new Error(`${r.status} ${data.error.code}: ${data.error.message}`);
  return data;
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function search(imageUrl, brief) {
  const task = await api("POST", "/searches", { image_url: imageUrl, brief: brief || "" });
  console.log("任务已提交：", task.task_id);
  for (;;) {
    const t = await api("GET", "/tasks/" + task.task_id);
    if (t.status === "succeeded") return api("GET", "/runs/" + t.run_id);
    if (t.status === "failed") throw new Error(t.error);
    console.log(`  [${t.status}] ${t.progress || "排队中，前面 " + t.queue_position + " 个"}`);
    await sleep(4000);
  }
}

async function main() {
  const run = await search(process.argv[2], process.argv[3]);
  console.log(`\n产品：${run.product.brand} · ${run.product.name}`);
  for (const d of run.dimensions) {
    console.log(`\n【${d.name}】参考什么：${d.what_to_learn}`);
    for (const v of d.videos.slice(0, 3))
      console.log(`  ${v.score}分 [${v.platform}] ${v.title}\n      ${v.url}\n      参考：${v.reference}`);
  }
}

main().catch(e => { console.error(e.message); process.exit(1); });
