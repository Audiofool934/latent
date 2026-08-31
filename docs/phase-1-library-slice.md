# Phase 1: Library slice

Date: 2026-08-31

## Current outcome

Latent 现在可以增量遍历一个真实 CloudDrive 档案目录树，并为其中的 ARW 建立持久化 preview job。
worker 可以分批处理 pending job，中断后的 running job 会在超时后重新进入队列，单张失败不会终止其余任务。

重复扫描使用远端内容指纹判断是否需要重新入队。
同一指纹已经成功完成时保持 succeeded，内容变化或用户显式要求重试时才重新排队。

本地 contact sheet 现在只读取 SQLite 与派生缓存，不创建 CloudDrive 客户端。
界面包含日期导航、缩略图网格、文件名/相机/镜头筛选、键盘选择和 EXIF inspector。
Contact 缓存决定照片是否可见，中尺寸 preview 被 LRU 淘汰时 inspector 会回退到 contact，因此缓存压力不会让照片从日期列表消失。

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

## Queue contract

- Claim 使用 SQLite `BEGIN IMMEDIATE`，避免两个 worker 同时取得同一个 job。
- Job 状态只有 pending、running、succeeded 和 failed。
- 每次 claim 增加 attempts，并记录 claimed time。
- 正常完成保存最终远端 fingerprint。
- 失败保存截断后的安全错误文本，用户可通过新的 scan 加 `--retry-failed` 重试。
- 超过恢复窗口的 running job 会回到 pending，支持进程退出后的继续执行。
- 所有索引、队列和缓存均属于可重建本地状态，不改变摄影档案。

## Next step

下一步是为 25,761 个 pending preview job 建立按最近日期优先的长期后台执行策略，并把吞吐量、预计剩余时间和暂停状态显示在 Library 界面。
完成首批近期日期后，再验证真实规模下的日期分页和缓存淘汰。
