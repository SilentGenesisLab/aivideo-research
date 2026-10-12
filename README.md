# 参考视频检索

上传产品图 → AI 拆解关键词 → 检索参考广告片地址 → 标注“参考什么” → 可选下载。

## 流程

```
产品图 (+ 可选简报)
 ① DeepSeek 识图 (deepseek-flash)：产品画像 + 5 个参考维度
    同品类 / 竞品 / 情绪主题 / 结构形式 / 获奖标杆，每个维度含“参考什么” + 中英文关键词
 ② 多源并发检索（约 60 次，十几秒）：
    YouTube（中英）· B 站（中）· 抖音（中，借必应视频）
    全网视频·中文（必应：B 站/抖音/爱奇艺/腾讯视频）· 海外广告库（必应：iSpot.tv/Ads of the World/The Drum…）
    统一地址跨来源去重，过滤时长（默认 5~300 秒）
 ③ DeepSeek 重排：0~10 打分、判断是否广告正片、写出具体参考点
    跨维度去重，维度内按平台轮流取，避免单一平台霸榜
 ④ 输出：网页卡片 + runs/<时间>/report.md + result.json
    站内播放：官方播放器内嵌（B 站/YouTube/优酷/腾讯/抖音/AcFun/Dailymotion），案例页自动挖播放器，
    其余站点由服务器拉取 480p 缓存（runs/_cache，上限 3GB）后播放
    另附 15 个“参考平台”：7 个直接列出作品（数英、Ads of the World、戛纳、新片场、iSpot.tv、抖音、花瓣），
    其余需登录的只给搜索页（Meta 广告库、TikTok Creative Center、巨量创意、小红书……）
    网页右上角“检索方法说明”（/method）介绍完整方法与拉片拆解表
 ⑤ 可选下载：检索后自动下载前 N 条或全部，也可以之后勾选下载
    yt-dlp 下载 + 合并为 H.264/AAC 的 mp4（默认 ≤720p）
```

## Docker 部署（推荐）

```bash
git clone git@github.com:SilentGenesisLab/aivideo-research.git && cd aivideo-research
cp .env_sample .env && chmod 600 .env           # 填入 DEEPSEEK_API_KEY、APP_PASSWORD
docker compose up -d --build                     # http://<服务器IP>:8770，用户名 admin
```

- 更新：`git pull && docker compose up -d --build`
- 日志：`docker compose logs -f`；停止：`docker compose down`
- 数据都在宿主机 `./runs`（检索结果、下载、播放缓存、任务记录），重建容器不丢；容器设置了 `restart: unless-stopped`，服务器重启后自动拉起。
- 端口、并发数在 `.env` 里改：`PORT`、`SEARCH_WORKERS`。

## 本机安装（不用 Docker）

```bash
cd aivideo-research
python3 -m venv --without-pip .venv            # 系统缺少 python3-venv 时的做法
pip3 --python .venv/bin/python install -r requirements.txt
cp .env_sample .env && chmod 600 .env           # 填入 DEEPSEEK_API_KEY、APP_PASSWORD
```

## 使用

**网页**

```bash
.venv/bin/python server.py                       # http://127.0.0.1:8765
./start.sh                                       # 后台对外运行在 8770 端口（需配置 APP_PASSWORD），./stop.sh 停止
```

配置 `APP_PASSWORD` 后网页启用登录（HTTP Basic Auth）；未配置时只允许本机访问。

检索、下载都是**异步任务**：提交后立即返回，可以连续提交多张产品图，页面“任务队列”显示排队和进度，完成后自动展示结果。
同时执行的检索数由 `SEARCH_WORKERS` 控制（默认 2），其余排队；任务记录保存在 `runs/_tasks/`，刷新页面不丢。

**命令行**

```bash
# 只检索
.venv/bin/python cli.py search 产品图.jpg --brief "30秒TVC，年轻白领，强调零糖"
# 检索并自动下载评分最高的 5 条
.venv/bin/python cli.py search 产品图.jpg --download top --top 5
# 之后按序号挑选下载
.venv/bin/python cli.py download 20261011-165350 --pick 1,4,7
```

**自测**

```bash
.venv/bin/python tests/selftest.py               # 离线单元测试
.venv/bin/python tests/selftest.py 产品图.jpg    # 加上在线端到端测试（约 1~3 分钟）
```

## 已知限制

- **参考点是推断的**：重排时 AI 只看到标题、频道、时长和播放量，看不到画面。参考点需要人看片核实。
- **B 站风控**：未登录时，高频搜索会被临时拦截。系统会自动降级到另一个接口并退避重试，仍失败的会记入“检索异常”，不影响整体结果。在 `.env` 配置 `BILIBILI_COOKIE` 可以大幅缓解。
- **抖音**：通过必应视频检索，可以拿到视频地址；下载需要在 `.env` 配置 `DOUYIN_COOKIES_FILE`（Netscape 格式 cookies.txt）。
- **下载支持**：YouTube、B 站、iSpot.tv、Ads of the World 可以下载；CulturePub 等站点不支持。单条下载超过 3 分钟会自动放弃。
- **Vimeo、广告门、小红书、巨量创意、TikTok Creative Center、Meta 广告库、FILMGRAB、Pinterest**：会拒绝服务器访问或需要登录，只提供搜索页链接。
- **站内播放**：YouTube 内嵌需要能访问 YouTube，否则用“服务器中转播放”；中转依赖 yt-dlp，部分站点（如新片场）会失败，此时请打开原页面。
- **成本与耗时**：一次检索约 10~15 次 DeepSeek 调用（1 次识图 + 每个维度 1~3 批打分）、约 60 次平台搜索，耗时约 2 分钟。
- **版权**：下载的视频仅供内部拉片学习，不要二次发布或商用。
