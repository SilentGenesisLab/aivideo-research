# 视频局部分析与生成预检

接口使用 Video Data 的 `X-API-Key`，并校验检索记录属于当前调用方。供应商凭据可由服务环境提供，也可随对应请求使用 `X-Ark-API-Key` 或 `X-AICentre-API-Key`。请求头中的凭据仅用于本次处理。

1. 下载参考片后，向 `POST /api/v1/runs/{run_id}/video-analyses` 提交原始 `url`。窗口预算 `max_windows` 默认 12。
2. 轮询返回的 `status_url`，完成后打开任务的 `result_url`。结果包含时间窗、事件、变化前后帧和带签名的帧表链接。`complete` 表示所选窗口是否均取得可解析结果。视频按原生画面变化选窗，每次同时分析两个局部片段。
3. 用 `GET /api/v1/generation/preflight?route=h3` 查询通道。若带有此前本账号的 H3 任务 ID，可用 `queue_reference` 查询全局等待数。全局等待数不可用时，结果会显示节点状态。
4. 向 `POST /api/v1/runs/{run_id}/video-analyses/{analysis_id}/generations` 提交要制作的 `event_ids` 和 `route`。请求由通过证据校验的事件编译成 4 秒纯文字制作说明。队列占满时返回 HTTP 409。相同内容返回已有回执，另做一个样本时可指定不同的 `take`。
5. 用 `GET /api/v1/runs/{run_id}/video-analyses/{analysis_id}/generations/{generation_id}` 查询已提交任务。普通生成通道的队列判断基于服务健康和当前分析中已跟踪的同通道任务。

需要逐次记录快速切换时，可向 `POST /api/v1/runs/{run_id}/video-analyses/{analysis_id}/transitions` 提交已有的 `window_id`。结果在分析的 `transition_events` 中，每条绑定相邻帧和源片时间，记录前后地表、脚步状态和动作衔接。可在生成的 `event_ids` 中选择这些事件，生成说明会按时刻写出每次变化。

结果记录所选窗口的画面观察。原生帧的像素变化用于定位，事件由视频和帧序列支撑。
