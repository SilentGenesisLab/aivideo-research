# 联调测试链接清单

全部链接于 **2026-11-04** 在 `https://videodata.sligenai.cn` 上实测通过：`POST /api/v1/resolve` 能解析，`GET stream_url` 能拉到首帧字节。

服务地址：`https://videodata.sligenai.cn`
鉴权：请求头 `X-API-Key: <你的 key>`

---

## 1. 单条解析 + 播放（17 条）

### 抖音（4）

| # | 链接 | 标题 | 时长 |
|---|---|---|---|
| 1 | `https://www.douyin.com/video/7461456374061436198` | 央视公益广告《Family》 | 90 s |
| 2 | `https://www.douyin.com/video/7130370497841204491` | 戛纳获奖《翻转》 | 67 s |
| 3 | `https://www.douyin.com/video/7214476922384928033` | 可口可乐《杰作》 | 102 s |
| 4 | `https://www.douyin.com/video/7656369709994609582` | 元气森林气泡水种草 | 17 s |

> 抖音首次解析要开无头浏览器，约 **8 秒**；30 分钟内重复请求命中缓存约 0.1 秒。抖音接口不返回清晰度（`max_height` 为 `null`，属正常）。

### 小红书（2）

| # | 链接 | 标题 | 清晰度 |
|---|---|---|---|
| 5 | `https://www.xiaohongshu.com/explore/6ab9f7f3000000001c00e91b?xsec_token=ABQBCaFZskGpZ9XHXgYvBdnt7xAPYXO71OkPhbGUwDlqs=&xsec_source=pc_search` | 挑战用AI生成产品广告大片 | 2560 |
| 6 | `https://www.xiaohongshu.com/explore/6a23ee7f0000000022017e93?xsec_token=AB0EO_6CIV4g54xia-TtA08BAo4sl1pKld14ndEHhgXUo=&xsec_source=pc_search` | AIGC品牌创意视频 | 1280 |

> ⚠️ **小红书链接必须整条带上 `xsec_token`**，token 会过期（通常几天）。过期后报 `400 resolve_failed`，这不是服务故障，换一条即可。只给 `/explore/<id>` 不带 token 会解析失败。

### B站（4）

| # | 链接 | 标题 | 清晰度 |
|---|---|---|---|
| 7 | `https://www.bilibili.com/video/BV1ip4y1z7oz` | 农夫山泉苏打水 产品视频创意拍摄 | 1080 |
| 8 | `https://www.bilibili.com/video/BV1e24y1Z7mt` | 短短15秒，却超级震撼 | 1080 |
| 9 | `https://www.bilibili.com/video/BV1WG411p7gs` | CCTV 中国亲子计划宣传片 | 720 |
| 10 | `https://www.bilibili.com/video/BV1g64y1i7s7` | 戛纳广告节金奖广告（视觉错视） | 592 |

> B站走出口代理解析，首帧约 0.1 秒。B站链接也支持短链 `https://b23.tv/xxxx`。

### YouTube（4）

| # | 链接 | 标题 | 清晰度 |
|---|---|---|---|
| 11 | `https://www.youtube.com/watch?v=7bu7wIVuwVE` | 可口可乐2023《Masterpiece杰作》 | 1080 |
| 12 | `https://www.youtube.com/watch?v=M-aoH-weg54` | Spotify: Let the Song Play（戛纳银狮） | 720 |
| 13 | `https://www.youtube.com/watch?v=1VM2eLhvsSM` | Coca-Cola 1971 'Hilltop' | 1080 |
| 14 | `https://www.youtube.com/watch?v=N4yvPF-z66k` | Bubly Sparkling Water 2021 | 720 |

> YouTube 1080p 没有音视频合一的直链，服务会自动合并成 mp4 并预取，所以首帧仍然很快。

### TikTok（3）

| # | 链接 | 清晰度 |
|---|---|---|
| 15 | `https://www.tiktok.com/@krystalturner9990/video/7697787413535444245` | 1920 |
| 16 | `https://www.tiktok.com/@huyseoul_idol/video/7701677826809859349` | 1920 |
| 17 | `https://www.tiktok.com/@hospital.behind/video/7700931562811985158` | 1280 |

> TikTok 走两跳代理出口，**首帧 3~5 秒**是正常的（出口带宽限制，不是服务卡住）。判断"能播"时请等够 10 秒。

---

## 2. 怎么测

### 2.1 解析（同步，2~10 秒）

```bash
curl -X POST https://videodata.sligenai.cn/api/v1/resolve \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://www.bilibili.com/video/BV1e24y1Z7mt"}'
```

返回里重点看三个字段：

- `title` / `author` / `duration` / `max_height` —— 元数据是否正确
- `stream_url` —— 不带 key 就能直接播，有效期 6 小时
- `platform` —— `douyin / xhs / bilibili / youtube / tiktok`

### 2.2 播放（不用 key）

`stream_url` 直接丢进浏览器或播放器即可，支持 `Range`（可拖进度条）：

```bash
curl -I "https://videodata.sligenai.cn/api/v1/resolve/stream?url=...&exp=...&sig=..."
# 期望：206 Partial Content
```

```html
<video src="返回的 stream_url" controls></video>
```

> 签名过期（超过 6 小时）返回 `403 forbidden`，重新调一次 `/resolve` 即可。

### 2.3 下载

**注意：下载接口只接受「某次检索结果里出现过」的视频地址**，随便传一个外来 URL 会返回
`400 invalid_request: 没有匹配到结果中的视频`。正确顺序是：

1. 先跑一次检索：

```bash
curl -X POST https://videodata.sligenai.cn/api/v1/searches \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"query":"气泡水 广告","depth":"quick","sources":["bilibili"],"target_count":3}'
```

2. 轮询任务直到 `status=succeeded`，拿到 `run_id`：

```bash
curl -H "X-API-Key: $API_KEY" \
  https://videodata.sligenai.cn/api/v1/tasks/<task_id>
```

3. 取结果里的视频地址（在 `dimensions[].videos[].url`）：

```bash
curl -H "X-API-Key: $API_KEY" \
  https://videodata.sligenai.cn/api/v1/runs/<run_id>
```

4. 提交下载：

```bash
curl -X POST https://videodata.sligenai.cn/api/v1/runs/<run_id>/downloads \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"urls":["https://www.bilibili.com/video/BV1Bu4y1q7Yy"],"max_height":720}'
```

5. 轮询任务，`result.downloads[]` 里每条带 `ok / size_mb / file`（`file` 是 `/files/...` 可下载地址）。

只想测单条下载、不想跑整轮检索的话，直接用 2.1 拿到的 `stream_url` 自己拉字节流即可。

---

## 3. 建议的测试覆盖

| 场景 | 期望 |
|---|---|
| 五个平台各解析一条 | 200，`title` 非空，`stream_url` 能出首帧 |
| 同一链接连续请求两次 | 第二次 <0.3 s（30 分钟缓存） |
| 传 `https://v.douyin.com/...` 短链 | 200（自动跟随跳转） |
| 传 `https://www.xiaohongshu.com/explore/xxx`（无 token） | 400 `resolve_failed`，属预期 |
| 传微信视频号链接 | 400 `resolve_failed`（没有公开网页，只支持元数据） |
| 不带 key 请求 | 401 `unauthorized` |
| 篡改 `stream_url` 的 `sig` | 403 `forbidden` |
| 下载不在结果里的 URL | 400 `invalid_request`，属预期 |

---

## 4. 已知限制

- 抖音、小红书需要服务器侧登录态 cookies，失效时抖音报"cookies 可能已失效"，需要重新导出。
- 抖音解析全局串行（一台机器只够开一个无头浏览器），并发请求会排队，不是失败。
- 抖音作品可能被作者删除或设为私密，此时返回 `400 resolve_failed` 并带上平台原文，换链接即可。
- 视频号只能返回检索时的元数据，不支持解析播放。
