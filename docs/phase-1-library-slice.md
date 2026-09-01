# Phase 1: Library slice

Date: 2026-08-31

## Current outcome

Latent 现在可以增量遍历一个真实 CloudDrive 档案目录树，并为其中的 ARW 建立持久化 preview job。
worker 可以分批处理 pending job，中断后的 running job 会在超时后重新进入队列，单张失败不会终止其余任务。

重复扫描使用远端内容指纹判断是否需要重新入队。
同一指纹已经成功完成时保持 succeeded，内容变化或用户显式要求重试时才重新排队。

本地 contact sheet 现在只读取 SQLite 与派生缓存，不创建 CloudDrive 客户端。
界面包含日期导航、缩略图网格、跨日期语义搜索、相似照片、键盘选择和 EXIF inspector。
Contact 缓存决定照片是否可见，中尺寸 preview 被 LRU 淘汰时 inspector 会回退到 contact，因此缓存压力不会让照片从日期列表消失。

首轮真实全库 worker 已经完成 25,793 个 preview job。
最初遗留的 3 个 CloudDrive HTTP 500 项在客户端升级后进行了一次有界重试，并全部成功。

## Commands

```bash
uv run latent scan --path '/CloudName/path/to/YYYY-MM-DD'
uv run latent scan-tree --path '/CloudName/archive-root' --max-directories 25
uv run latent scan-tree --scan-id 1 --max-directories 100
uv run latent scan-tree-cancel --scan-id 1
uv run latent work --max-jobs 25
uv run latent status
uv run latent serve
```

`scan` 只调用 CloudDrive 目录和文件元数据接口。
`scan-tree` 消费 CloudDrive `GetSubFiles` 的完整服务端分页流，并在每个目录边界持久化进度。
`work` 继续使用 Phase 0 的严格 Range 管线，任何完整文件响应都会在读取 body 前被拒绝。

## Full archive metadata evidence

真实档案根目录为 `/阿里云盘Open/📷 - Photography`。
本轮只读取目录与文件元数据，没有读取任何 RAW body。

完整 scan `#1` 的最终状态：

- 217 个目录 succeeded
- 0 个 pending、running 或 failed 目录
- 25,793 个 ARW 条目 discovered
- 25,761 个 preview job enqueued
- 32 个已有任务保持 unchanged
- 21 个名称以 `.` 或 `_` 开头的辅助目录被排除
- 约 166 秒完成三次可恢复批次
- 0 次 archive write

根级 `_exports`、`_inbox` 和 `_metadata` 默认不进入正常图库。
这个边界避免把尚未合并的修复材料、派生输出和元数据副本当成正式照片重复索引。

扫描可以通过 `--max-directories` 在确定的目录边界停下，再使用 `--scan-id` 恢复。
`scan-tree-cancel` 会把 scan 标为 cancelled，当前目录完成后停止，后续仍可从同一个 scan ID 恢复。
失败目录单独记录并支持 `--retry-failed`，已成功目录不会重复处理。

## Live directory evidence

验证目录包含 32 张 Sony A7R II ARW。

首次扫描结果：

- 32 张 ARW discovered
- 32 个 preview job enqueued
- 0 个 failed 或 running 残留

worker 完成后，从 SQLite 获取到的批次指标为：

- 32 个 job processed
- 32 个 succeeded
- 1 个已有 cache hit
- 31 个 cold preview fetch
- 18,135,563 bytes transferred
- 平均 metadata 加 pipeline 时间 963.7 ms
- 最慢单张 2,107 ms
- 0 次 archive write

第二次扫描同一目录时，32 张均为 unchanged，新增 job 为 0。

## Contact sheet evidence

本地服务使用已经生成的 34 组 contact 与 preview 缓存进行验收，全程未访问 CloudDrive。
主要验证日期为 `2025-12-25`，包含 32 张真实 Sony A7R II ARW。

桌面 `1440x900` 状态验证结果：

- 32 张缩略图全部成功加载
- contact sheet 为五列，无页面或网格横向溢出
- 初始选中、点击选择、文件名筛选与 inspector 元数据均正确
- 方向键可以从当前卡片移动到相邻卡片

窄屏与紧凑状态验证结果：

- `1024x768` 下 inspector 变为右侧可关闭覆盖层
- `720x900` 下隐藏固定侧栏，显示日期选择器，并以四列呈现 32 张缩略图
- 日期切换会同步标题、卡片数量与 URL
- 预览使用 `object-fit: contain`，不会裁切照片
- 浏览器控制台没有 warning 或 error

HTTP 边界测试确认静态资源、JSON API 与缓存 JPEG 都能从 wheel 正常提供。
服务默认拒绝非 loopback 地址，媒体路由拒绝路径穿越，并为响应设置 CSP 与基础安全头。
额外测试确认中尺寸 preview 被淘汰后，contact-only 照片仍保留在日期和 contact sheet 中。

## Full archive preview evidence

首轮真实全库运行的最终状态为：

- 25,793 个 preview job succeeded
- 0 个 pending、running 或 failed job
- 25,793 个 contact cache entry，占用 608,759,497 bytes
- 17,724 个中尺寸 preview cache entry，占用 4,294,870,531 bytes
- 全部 fetch run 累计传输 15,566,916,758 bytes，约 14.50 GiB
- 0 次 archive write

中尺寸 preview 缓存已经稳定在 4 GiB LRU 上限附近，因此其 entry 数量少于全库总数是预期行为。
Contact cache 保留全库覆盖，确保被 LRU 淘汰的照片仍可在日期浏览器中出现。

CloudDrive 1.0.16 的 macOS 按需流式读取能力不会替代这条索引管线。
批量预览继续使用严格 Range 读取，以获得明确的字节上限和完整响应拒绝机制。
macFUSE 挂载路径只作为未来 DxO 原片 handoff 的候选通道，并需要以未缓存 RAW 验证具体启动方式。
Finder 双击或“打开方式”可能触发完整下载，因此不属于可接受的流式 handoff 证据。
CloudDrive 已读取区段的本地缓存与 Latent 派生预览缓存是两个独立的空间预算。

## High-volume date pagination evidence

日期资产 API 现在返回筛选后的准确总数、是否仍有下一页，以及下一页 offset。
网页首批读取 250 张，滚动接近底部或点击 Load more 时继续追加，而不是一次创建数千个图片节点。
当前日期内的文件名、相机和镜头搜索由 SQLite 执行，因此可以命中尚未加载到 DOM 的照片。

真实日期 `2025-12-13` 的验证结果为：

- 日期总数 2,656 张
- 首批返回 250 张，下一页 offset 为 250
- 最后一批从 offset 2,500 返回 156 张
- 页面最终包含 2,656 张卡片，Load more 自动结束
- 对最后一批文件名的大小写不敏感搜索成功命中
- `1440x900` 为五列布局，无页面或网格横向溢出
- `390x844` 为两列布局，日期选择、搜索和 Inspector 开关均正常

## Semantic discovery evidence

全局搜索不再依赖 `DSCxxxxx` 文件名。
服务使用独立的 SigLIP2 vector store，把自然语言 query 与全部已完成的本地 contact embedding 做 cosine ranking。
相似照片直接使用已保存的图像向量，并从结果中排除源图。

HTTP 与界面 smoke test 使用 3 张真实 contact preview 和隔离的临时 embedding store。
它没有修改正式的 25,793 项 production queue。

实测结果为：

- 中文查询 `天空中飞翔的鸟` 冷启动 HTTP 总耗时 6.638 秒，返回 3 条完整资产记录
- 模型保持在同一服务进程后，中文查询 `夜晚城市灯光` 总耗时 0.474 秒
- 从真实资产请求相似照片耗时 4.7 毫秒，源图被排除
- 搜索响应包含 rank、cosine similarity、模型 ID、local contact source 和解释边界
- `1440x900` 桌面界面完成语义搜索和相似照片真实手势验证，无横向溢出
- `390x844` 手机界面完成语义搜索、两列结果和 Inspector 覆盖层真实手势验证，无横向溢出
- 两种视口的浏览器控制台均没有 warning 或 error

3 张样本只能证明 text encoder、vector ranking、Library metadata 和 HTTP/UI 的端到端连接。
它不能证明全库检索质量，也不能支持有意义的 Curator 判断。
正式 store 仍为 25,793 pending、0 succeeded、0 failed 和 0 vectors，必须在用户确认资源预算后才会启动全库构建。

所有返回关系都明确标注为模型证据，不是地点、人物身份或故事的证明。
服务只读取本地 SQLite、contact cache、embedding store 和本地模型目录，不读取 RAW、不访问 CloudDrive，也不写入 aDrive。

## Queue contract

- Claim 使用 SQLite `BEGIN IMMEDIATE`，避免两个 worker 同时取得同一个 job。
- Job 状态只有 pending、running、succeeded 和 failed。
- 每次 claim 增加 attempts，并记录 claimed time。
- 正常完成保存最终远端 fingerprint。
- 失败保存截断后的安全错误文本，用户可通过新的 scan 加 `--retry-failed` 重试。
- 超过恢复窗口的 running job 会回到 pending，支持进程退出后的继续执行。
- Worker 启动时优先使用路径中的 `YYYY-MM-DD` 排序，缺少标准日期目录时回退到远端 write time。
- 连续五个 job 失败时 worker 自动停止，避免 provider 断线导致整条队列被批量标记失败。
- `Ctrl-C` 会立即把当前 running job 放回 pending，不需要等待 stale recovery 窗口。
- `work --retry-failed` 可以把失败任务重新放回 pending。
- 所有索引、队列和缓存均属于可重建本地状态，不改变摄影档案。

## Next step

下一步是在资源预算确认后构建全库 embedding，并实现受证据约束的 Curator、Sequence 与可独立备份的 writable workspace。
DxO handoff 需要先对一张未缓存 RAW 做启动路径、首屏等待、实际读取量和本地缓存增长的端到端测试。
