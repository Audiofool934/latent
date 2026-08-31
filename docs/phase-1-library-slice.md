# Phase 1: Library slice

Date: 2026-08-31

## Current outcome

Latent 现在可以把一个真实 CloudDrive 日期目录扫描进 SQLite，并为其中的 ARW 建立持久化 preview job。
worker 可以分批处理 pending job，中断后的 running job 会在超时后重新进入队列，单张失败不会终止其余任务。

重复扫描使用远端内容指纹判断是否需要重新入队。
同一指纹已经成功完成时保持 succeeded，内容变化或用户显式要求重试时才重新排队。

## Commands

```bash
uv run latent scan --path '/CloudName/path/to/YYYY-MM-DD'
uv run latent work --max-jobs 25
uv run latent status
```

`scan` 只调用 CloudDrive 目录和文件元数据接口。
`work` 继续使用 Phase 0 的严格 Range 管线，任何完整文件响应都会在读取 body 前被拒绝。

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

## Queue contract

- Claim 使用 SQLite `BEGIN IMMEDIATE`，避免两个 worker 同时取得同一个 job。
- Job 状态只有 pending、running、succeeded 和 failed。
- 每次 claim 增加 attempts，并记录 claimed time。
- 正常完成保存最终远端 fingerprint。
- 失败保存截断后的安全错误文本，用户可通过新的 scan 加 `--retry-failed` 重试。
- 超过恢复窗口的 running job 会回到 pending，支持进程退出后的继续执行。
- 所有索引、队列和缓存均属于可重建本地状态，不改变摄影档案。

## Next step

下一步是在完全不访问 CloudDrive 的情况下，从本地 SQLite 和缓存渲染三栏 contact sheet。
这将同时验证离线浏览、网格密度、键盘选择和 inspector 信息层级。
