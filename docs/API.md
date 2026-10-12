# 参考视频检索 API 接入文档（v1）

把“上传产品图 → 找参考广告片”作为模块接入你自己的网站。

- **在线调试**：[https://<服务地址>/api/v1/docs](https://<服务地址>/api/v1/docs)（Swagger，点 Authorize 填 API Key 即可直接调用）
- **OpenAPI 规范**：[https://<服务地址>/api/v1/openapi.json](https://<服务地址>/api/v1/openapi.json)（可导入 Postman/Apifox，或生成各语言 SDK）
- **示例代码**：[Python](https://<服务地址>/developer/examples/python_client.py) · [Node.js](https://<服务地址>/developer/examples/node_client.mjs)

---

## 1. 它能做什么

输入一张产品图（可加一段简报），返回：

| 内容 | 说明 |
|---|---|
| 产品画像 | 品牌、产品名、品类、外观、卖点、目标人群、调性、竞品 |
| 5 个参考维度 | 同品类 / 竞品 / 情绪主题 / 结构形式 / 获奖标杆，每个维度写明“参考什么”和检索关键词 |
| 参考视频 | 每个维度最多 10 条，来自 B 站、抖音、优酷、YouTube、数英、iSpot.tv 等 16 个渠道；每条有 AI 评分（0~10）、是否广告正片、**具体参考点**、可内嵌的播放地址 |
| 参考平台 | 15 个人工补充检索的平台及搜索链接，其中 7 个可通过接口直接取作品 |
| 可选下载 | 指定视频下载为 mp4（H.264/AAC），返回限时下载链接 |

一次检索约 **1~3 分钟**，所以所有耗时操作都是**异步任务**。

---

## 2. 接入方式

```
你的浏览器页面 ──▶ 你的后端 ──(X-API-Key)──▶ 本服务 /api/v1
        ▲                │                           │
        └── 展示结果 ◀────┘ ◀──── Webhook 回调 ─────────┘
```

- **由你的后端调用**本接口，API Key 只放在你的服务器上，**不要写进前端代码**。
- 结果中的图片、视频文件都是**免登录的限时签名链接**，可以直接交给浏览器的 `<img>` / `<video>`。
- 结果中的 `embed_url` 是各视频平台的官方播放器地址，可以直接放进 `<iframe>`。

---

## 3. 五分钟上手

**① 找服务方要一个 API Key**（形如 `sk_live_xxx`）。

**② 创建检索任务**

```bash
curl -X POST https://<服务地址>/api/v1/searches \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{
    "image_url": "https://your-cdn.com/products/123.jpg",
    "brief": "30秒TVC，年轻白领，强调零糖，想参考元气森林",
    "external_id": "project-123"
  }'
```

立即返回（HTTP 202）：

```json
{
  "task_id": "9f1c2a7b3d4e",
  "status": "queued",
  "status_url": "https://<服务地址>/api/v1/tasks/9f1c2a7b3d4e"
}
```

**③ 等结果**：每 3~5 秒轮询 `status_url`，直到 `status` 为 `succeeded` 或 `failed`；然后用 `result_url` 取结果。

```bash
curl -H "X-API-Key: $API_KEY" https://<服务地址>/api/v1/tasks/9f1c2a7b3d4e
# → {"status": "succeeded", "run_id": "20261012-074744", "result_url": ".../api/v1/runs/20261012-074744", ...}
curl -H "X-API-Key: $API_KEY" https://<服务地址>/api/v1/runs/20261012-074744
```

也可以在创建时传 `callback_url`，任务结束时本服务主动通知你（见 [第 7 节](#7-webhook-回调)），不用轮询。

---

## 4. 通用约定

### 4.1 鉴权

每个请求带以下任一请求头：

```
X-API-Key: <key>
Authorization: Bearer <key>
```

- 每个 Key 对应一个接入方，**只能看到自己创建的任务和结果**，访问别人的会返回 404。
- `/api/v1/docs`、`/api/v1/openapi.json` 不需要 Key。

### 4.2 错误格式

所有错误都是同一结构，用 `code` 做程序判断，`message` 给人看：

```json
{ "error": { "code": "invalid_sources", "message": "未知渠道：xxx，可用值见 GET /api/v1/sources" } }
```

| HTTP | code | 含义 |
|---|---|---|
| 400 | `invalid_image` | 没传图片、两种方式都传了、格式不是 jpg/png/webp、base64 无效 |
| 400 | `image_too_large` | 图片超过 15MB |
| 400 | `image_fetch_failed` | `image_url` 下载失败 |
| 400 | `invalid_sources` | `sources` 里有未知渠道 |
| 400 | `invalid_callback_url` / `invalid_url` | 地址不是 http(s) 或指向内网 |
| 400 | `invalid_request` | 其他业务参数错误 |
| 401 | `unauthorized` | 缺少或无效的 API Key |
| 404 | `not_found` | 任务/结果不存在，或不属于你的 Key |
| 422 | `invalid_request` | 参数类型或取值范围错误（message 会写明字段） |
| 502 | `upstream_error` | 第三方平台抓取失败 |
| 500 | `internal_error` | 服务内部错误 |

### 4.3 任务状态

```
queued（排队）──▶ running（执行中）──▶ succeeded（成功）
                                    └──▶ failed（失败，看 error）
```

- 同时执行的检索数有上限（默认 2 个），超出的排队，`queue_position` 表示前面还有几个。
- 服务重启时，未完成的任务会变为 `failed`（error：“服务重启，任务中断”），需要重新提交。

### 4.4 文件链接

`product_image_url`、`report_url`、`download.file_url`、`play_url` 都是签名链接：

- 无需 API Key，可直接给浏览器使用；支持 Range（视频可拖动进度）。
- **默认 7 天后失效**。需要长期保存的文件请下载到你自己的存储；过期后重新调用 `GET /runs/{run_id}` 即可拿到新链接。

---

## 5. 接口列表

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/sources` | 检索渠道列表 |
| POST | `/api/v1/searches` | 创建检索任务（JSON，图片用 URL 或 base64） |
| POST | `/api/v1/searches/upload` | 创建检索任务（multipart 上传图片） |
| GET | `/api/v1/tasks/{task_id}` | 查询任务 |
| GET | `/api/v1/tasks?limit=20` | 最近的任务 |
| GET | `/api/v1/runs/{run_id}` | 获取检索结果 |
| GET | `/api/v1/runs?limit=20` | 历史检索 |
| POST | `/api/v1/runs/{run_id}/rerun` | 用同一张图重新检索（可换渠道/简报） |
| POST | `/api/v1/runs/{run_id}/downloads` | 下载结果中的视频 |
| GET | `/api/v1/play?url=` | 解析视频的播放方式 |
| POST | `/api/v1/relay` | 服务器中转播放（不能内嵌的站点） |
| GET | `/api/v1/reference-platforms/items?name=&q=` | 直接取某个参考平台的作品 |

### 5.1 GET /sources

```json
[
  {"key": "bilibili", "name": "B 站", "group": "国内视频", "languages": ["zh"]},
  {"key": "youtube", "name": "YouTube", "group": "海外视频", "languages": ["en", "zh"]},
  {"key": "digitaling", "name": "数英", "group": "广告案例库", "languages": ["zh"]}
]
```

当前 16 个渠道：`bilibili` `douyin` `youku` `tencent` `iqiyi` `acfun` `web_cn` `youtube` `dailymotion` `web_intl` `digitaling` `xinpianchang` `ispot` `adsoftheworld` `adweek` `culturepub`。以接口返回为准。

### 5.2 POST /searches

请求体（JSON）：

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `image_url` | string | — | 产品图公网地址，与 `image_base64` **二选一** |
| `image_base64` | string | — | 产品图 base64，可带 `data:image/jpeg;base64,` 前缀 |
| `brief` | string | `""` | 简报：目标、人群、时长、调性、想参考的竞品。**越具体结果越准** |
| `sources` | string[] | 全部 | 检索渠道 key |
| `per_query` | int 1~20 | 8 | 每个关键词每个渠道取几条，调大更全但更慢 |
| `max_duration` | int 10~1800 | 300 | 视频最长秒数；只要 TVC 建议 90 |
| `top_per_dim` | int 1~30 | 10 | 每个维度最多保留几条 |
| `min_score` | int 0~10 | 5 | 低于此评分的丢弃 |
| `download` | `none`/`top`/`all` | `none` | 检索完是否顺带下载 |
| `download_top` | int 1~50 | 5 | `download=top` 时下载几条 |
| `max_height` | 360/480/720/1080 | 720 | 下载清晰度上限 |
| `callback_url` | string | — | 任务结束时的 Webhook 地址 |
| `external_id` | string ≤128 | — | 你的业务 ID，会原样出现在任务、结果和回调里 |

返回 202：`{"task_id", "status": "queued", "status_url"}`

### 5.3 POST /searches/upload

`multipart/form-data`，字段与 5.2 相同，区别：

- 图片用文件字段 `image` 上传；
- `sources` 用逗号分隔的字符串，如 `bilibili,youku,digitaling`。

```bash
curl -X POST https://<服务地址>/api/v1/searches/upload -H "X-API-Key: $API_KEY" \
  -F image=@product.jpg -F brief="30秒TVC，年轻白领" -F sources=bilibili,youku,digitaling
```

### 5.4 GET /tasks/{task_id}

```json
{
  "task_id": "9f1c2a7b3d4e",
  "type": "search",
  "status": "running",
  "title": "30秒TVC，年轻白领，强调零糖",
  "queue_position": 0,
  "progress": "③ AI 评估相关性并标注参考点…",
  "created_at": "2026-10-12 07:47:44",
  "started_at": "2026-10-12 07:47:44",
  "finished_at": null,
  "error": null,
  "run_id": null,
  "result_url": null,
  "play_url": null,
  "external_id": "project-123"
}
```

- `type`：`search` 检索 / `download` 下载 / `relay` 中转播放
- 成功后 `run_id`、`result_url` 有值；`relay` 任务成功后 `play_url` 有值。

### 5.5 GET /runs/{run_id}（结果）

```json
{
  "run_id": "20261012-074744",
  "created_at": "2026-10-12 07:49:49",
  "external_id": "project-123",
  "brief": "30秒TVC，年轻白领，强调零糖",
  "sources": ["bilibili", "youku", "digitaling"],
  "product_image_url": "https://<服务地址>/files/20261012-074744/product.jpg?exp=...&sig=...",
  "report_url": "https://<服务地址>/files/20261012-074744/report.md?exp=...&sig=...",
  "product": {
    "brand": "Schweppes（舒味思）",
    "name": "Schweppes Zero Sugar sparkling water",
    "category": "零糖气泡水",
    "appearance": "透明PET瓶，黄色旋盖，绿色主标签……",
    "selling_points": ["零糖零负担", "强劲气泡", "百年品牌背书"],
    "target_audience": "20-35岁城市年轻白领……",
    "tone": "清爽、明亮、快节奏、轻幽默",
    "competitors": ["Perrier 巴黎水", "元气森林 气泡水", "bubly"]
  },
  "dimensions": [
    {
      "key": "category",
      "name": "同品类",
      "what_to_learn": "学同类气泡水广告如何把“气泡”视觉化成主角：开瓶喷涌、液体微距……",
      "queries_zh": ["气泡水广告", "苏打水 TVC 广告片"],
      "queries_en": ["sparkling water commercial"],
      "videos": [
        {
          "platform": "YouTube",
          "url": "https://www.youtube.com/watch?v=drZIKbUMEuY",
          "title": "Schweppes Zero Calories Sparkling Water",
          "channel": "Schweppes India",
          "duration": 16,
          "views": 14322903,
          "thumbnail": "https://i.ytimg.com/vi/drZIKbUMEuY/hq720.jpg",
          "published": "",
          "score": 10,
          "is_ad": true,
          "reference": "官方零卡气泡水16秒正片，学其清爽视觉与气泡动能表达",
          "reason": "本品牌同类正片，最佳对标",
          "embed_url": "https://www.youtube.com/embed/drZIKbUMEuY?autoplay=1&rel=0",
          "download": {
            "ok": true,
            "file_url": "https://<服务地址>/files/20261012-074744/videos/YouTube_drZIKbUMEuY_....mp4?exp=...&sig=...",
            "size_mb": 3.2,
            "error": null
          }
        }
      ]
    }
  ],
  "reference_platforms": [
    {
      "name": "数英网 DIGITALING",
      "kind": "creative",
      "kind_name": "优秀创意（找“好”的标准）",
      "learn": "国内品牌 TVC 案例与创意说明，看洞察和创意阐述",
      "query": "气泡水广告",
      "search_url": "https://www.bing.com/search?q=site%3Adigitaling.com+...",
      "direct": true
    }
  ],
  "stats": {"queries": 170, "raw": 814, "kept": 50, "seconds": 125.0},
  "errors": []
}
```

字段说明：

- `dimensions[].key`：`category` 同品类、`competitor` 竞品、`emotion` 情绪主题、`structure` 结构形式、`award` 获奖标杆。
- `videos[].reference` 是 AI 根据标题和简介推断的参考点，**没有看画面**，展示时建议注明“请看片核实”。
- `videos[].embed_url` 为 `null` 表示该站不能直接内嵌，见 [第 6 节](#6-在你的页面里播放视频)。
- `videos[].download` 为 `null` 表示没下载过。
- `errors`：个别渠道检索失败的说明，不影响其他结果。

### 5.6 POST /runs/{run_id}/rerun

用同一张产品图重新检索，常用于换渠道或改简报。

```json
{"sources": ["bilibili", "douyin"], "brief": "改成15秒短视频", "callback_url": "...", "external_id": "..."}
```

所有字段可选：`sources` 不传则用全部渠道，`brief` 不传则沿用原简报。返回新的 `task_id`，会生成新的 `run_id`。

### 5.7 POST /runs/{run_id}/downloads

```json
{"urls": ["https://www.youtube.com/watch?v=drZIKbUMEuY"], "max_height": 720, "callback_url": "..."}
```

或 `{"top": 5}` 下载评分最高的 5 条。返回 `task_id`；完成后重新 `GET /runs/{run_id}`，对应视频的 `download.file_url` 即为 mp4。

- 单条超过 3 分钟未完成会放弃，`download.error` 写明原因。
- 抖音需要服务方配置 cookies；新片场、CulturePub 等少数站点不支持下载。

### 5.8 GET /play?url=

```json
{"kind": "iframe", "src": "https://player.bilibili.com/player.html?bvid=BV1Bu4y1q7Yy&autoplay=1"}
```

| kind | 用法 |
|---|---|
| `iframe` | `src` 放进 `<iframe>` |
| `video` | `src` 是 mp4 直链，放进 `<video>` |
| `relay` | 不能直接播放，调 `POST /relay` 由服务器中转 |

### 5.9 POST /relay

```json
{"url": "https://www.iqiyi.com/v_19rzkibqdo.html"}
```

返回 `task_id`，通常 10~60 秒完成，任务的 `play_url` 是 480p mp4，放进 `<video>` 播放。同一视频第二次中转直接命中缓存。也适用于用户网络打不开 YouTube 的情况。

### 5.10 GET /reference-platforms/items?name=&q=

`name` 取 `reference_platforms[].name`（仅 `direct=true` 的平台），`q` 一般用对应的 `query`。

```json
[
  {"platform": "数英", "url": "https://www.digitaling.com/projects/189594.html", "title": "元气森林：冒泡吧，出嗝青年！",
   "thumbnail": "https://...", "duration": null, "media": "video"},
  {"platform": "花瓣", "url": "https://huaban.com/pins/6762848414", "title": "蓝色夏季冰爽海报",
   "thumbnail": "https://...", "duration": null, "media": "image"}
]
```

`direct=false` 的平台（Meta 广告资料库、巨量创意、小红书等）需要登录，只能打开 `search_url`。

---

## 6. 在你的页面里播放视频

```js
async function play(video, container) {
  if (video.download?.ok) {                 // 已下载：播放本地文件，最稳定
    container.innerHTML = `<video src="${video.download.file_url}" controls autoplay></video>`;
  } else if (video.embed_url) {             // 官方播放器
    container.innerHTML = `<iframe src="${video.embed_url}" allow="autoplay; fullscreen" allowfullscreen></iframe>`;
  } else {                                  // 经你的后端转调 /play，必要时再调 /relay
    const r = await fetch(`/your-backend/play?url=${encodeURIComponent(video.url)}`).then(r => r.json());
    // r.kind === "iframe" / "video" / "relay"
  }
}
```

- 抖音播放器是竖屏，容器建议 9:16。
- 缩略图来自各平台，`<img>` 建议加 `referrerpolicy="no-referrer"`，否则部分平台会拒绝外链。
- 渲染标题、参考点等文本时请做 HTML 转义，内容来自第三方平台。

---

## 7. Webhook 回调

创建任务时传 `callback_url`，任务结束（成功或失败）时本服务会 `POST` 到该地址：

```http
POST /your/callback HTTP/1.1
Content-Type: application/json
X-Webhook-Id: 9f1c2a7b3d4e
X-Webhook-Timestamp: 1791791401
X-Webhook-Signature: sha256=5d41402abc4b2a76b9719d911017c592...

{"event": "task.succeeded", "task": { ...与 GET /tasks/{id} 相同... }}
```

- `event`：`task.succeeded` 或 `task.failed`。
- 你的接口返回 **2xx** 即视为送达；否则在 5 秒、30 秒、2 分钟后重试，共 4 次。
- 可能重复送达，请用 `X-Webhook-Id`（即 task_id）去重。
- 回调只是通知，拿到后用 `task.result_url` 取完整结果。
- `callback_url` 必须是公网可访问的 http(s) 地址。

### 验证签名

服务方配置了 `WEBHOOK_SECRET` 时会带 `X-Webhook-Signature`：

```
签名 = "sha256=" + HMAC_SHA256(WEBHOOK_SECRET, 时间戳 + "." + 原始请求体)
```

请用**原始请求体字节**计算（不要先解析再序列化），并拒绝时间戳与当前相差超过 5 分钟的请求。

**Node.js（Express）**

```js
import crypto from "node:crypto";
app.post("/callback", express.raw({type: "application/json"}), (req, res) => {
  const ts = req.get("X-Webhook-Timestamp");
  const want = "sha256=" + crypto.createHmac("sha256", process.env.WEBHOOK_SECRET)
                                 .update(ts + ".").update(req.body).digest("hex");
  const got = req.get("X-Webhook-Signature") || "";
  if (got.length !== want.length || !crypto.timingSafeEqual(Buffer.from(got), Buffer.from(want))
      || Math.abs(Date.now() / 1000 - Number(ts)) > 300) return res.sendStatus(401);
  const {event, task} = JSON.parse(req.body);
  // ……按 task.external_id 更新你的业务数据
  res.sendStatus(200);
});
```

**Python（Flask）**

```python
import hashlib, hmac, os, time
from flask import Flask, request, abort

@app.post("/callback")
def callback():
    ts, body = request.headers["X-Webhook-Timestamp"], request.get_data()
    want = "sha256=" + hmac.new(os.environ["WEBHOOK_SECRET"].encode(), ts.encode() + b"." + body,
                                hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, request.headers.get("X-Webhook-Signature", "")) \
            or abs(time.time() - int(ts)) > 300:
        abort(401)
    data = request.get_json()
    # ……按 data["task"]["external_id"] 更新你的业务数据
    return "", 200
```

---

## 8. 性能与限制

| 项 | 说明 |
|---|---|
| 检索耗时 | 全部 16 个渠道约 1~3 分钟；只选 3~5 个渠道约 30~60 秒 |
| 并发 | 同时执行的检索默认 2 个（服务方可调），其余排队 |
| 轮询频率 | 建议 3~5 秒一次 |
| 图片 | jpg / png / webp，≤ 15MB；包装正面、背景干净时识别最准 |
| 结果稳定性 | 来自第三方平台的实时搜索，同样的输入每次结果会有差异；个别渠道偶尔失败会记录在 `errors` |
| 版权 | 下载的视频仅供内部参考学习，不要二次发布或商用 |

---

## 9. 服务方配置（部署者看）

在服务的 `.env` 中配置：

| 变量 | 必填 | 说明 |
|---|---|---|
| `API_KEYS` | 是 | 接入方与 Key，格式 `站点A:key1,站点B:key2`。生成 Key：`python3 -c "import secrets;print('sk_live_'+secrets.token_urlsafe(24))"` |
| `WEBHOOK_SECRET` | 建议 | Webhook 签名密钥，提供给接入方用于验签 |
| `PUBLIC_BASE_URL` | 建议 | 对外地址，如 `https://ref.example.com`。经反向代理或 HTTPS 部署时必须配置，否则返回的链接地址不对 |
| `SIGNING_SECRET` | 否 | 文件签名密钥；不配置时自动生成并保存在 `runs/.signing_secret` |
| `FILE_URL_TTL` | 否 | 文件链接有效期（秒），默认 604800（7 天） |
| `SEARCH_WORKERS` | 否 | 同时执行的检索数，默认 2 |

修改后重启服务（`docker compose up -d`）。对外提供时建议在前面加 HTTPS 反向代理（Nginx/Caddy），API Key 不要走明文 HTTP。

验证接入是否正常：在服务目录运行 `python tests/api_test.py 产品图.jpg`（覆盖鉴权、检索、回调验签、下载、播放全流程）。
