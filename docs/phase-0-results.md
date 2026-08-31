# Phase 0: Read-only preview spike

Date: 2026-08-31

## Outcome

Latent 已经能绕过 Finder 和 macFUSE 文件物化，通过 CloudDrive 本地 API 找到远端 ARW，再用严格受限的 HTTP Range 读取相机内嵌 JPEG。
生成的 screen preview 和 contact thumbnail 会原子写入本地分层缓存，文件、EXIF、预览位置、缓存状态和每次获取指标会同步写入 SQLite。

这条链路不会调用任何远端写入、移动、重命名或删除接口。
每次获取记录的 `archive_modified` 固定为 `0`，程序也会在服务端忽略 Range 或返回不同字节范围时停止，避免意外下载完整 RAW。

## Implementation boundary

Phase 0 使用 Python 3.11 是为了直接验证现有 CloudDrive Python client 和 ExifTool 路径。
这不是最终 GUI 技术栈决定。

核心边界如下：

1. `CloudDriveRangeSource` 只负责远端元数据和精确字节范围。
2. `ExifToolProbe` 只读取已下载到本地临时文件的有限 RAW 前缀。
3. `PreviewPipeline` 发现内嵌 JPEG，修正方向，并生成 2560 px 上限的 screen preview 与 512 px contact thumbnail。
4. `StateStore` 保存可重建的 SQLite 索引和不可伪造为远端写入的获取日志。
5. `CacheManager` 使用原子替换、内容指纹失效和按层 LRU 预算。

默认缓存预算与产品定义一致：contact 3 GiB、screen preview 4 GiB、temporary 1 GiB。

## Live evidence

三张样本均直接来自 CloudDrive 远端路径，没有通过挂载目录打开 RAW。
这些数字是同一台 Mac 和当前网络条件下的单次技术验证，不是稳定性能承诺。

| Camera and orientation | RAW bytes | Range bytes | RAW fraction | End-to-end | Embedded preview |
|---|---:|---:|---:|---:|---:|
| Sony A7R V, landscape | 77,541,376 | 706,248 | 0.911% | 1,274 ms | 1616 x 1080 |
| Sony A7R II, landscape | 43,122,688 | 556,880 | 1.291% | 903 ms | 1616 x 1080 |
| Sony A7R II, portrait | 43,220,992 | 657,844 | 1.522% | 851 ms | 1080 x 1616 |

The A7R V sample used two HTTP Range requests.
Its embedded preview began at byte 196,770 and occupied 509,478 bytes.
The complete preview path, including CloudDrive metadata lookup, finished below the 1.5 second Phase 0 target.

Two later runs of the same A7R V sample both produced cache hits with 0 Range requests and 0 media bytes transferred.
Provider metadata varied from 34 to 377 ms, the local pipeline took 1 to 2 ms, and measured end-to-end time varied from 35 to 379 ms.

Visual inspection confirmed that the A7R V landscape sample and A7R II portrait sample were decoded at the correct orientation, without cropping or aspect-ratio distortion.

SQLite ended with:

- `PRAGMA integrity_check`: `ok`
- 3 indexed assets
- 3 screen previews totaling 1,071,518 bytes
- 3 contact thumbnails totaling 99,333 bytes
- No temporary cache files

## Automated contracts

The test suite covers:

- A cold fetch that transfers only bounded ranges
- Refusal before body read when a provider ignores Range and attempts a full response
- A warm fetch with zero media-byte network activity
- SQLite asset and fetch-run persistence
- Per-tier LRU eviction
- Remote fingerprint changes invalidating stale cache files

Run the checks with:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

## Remaining Phase 0 risks

- The live sample covers Sony A7R II and A7R V ARW files, not every RAW family present in the archive.
- JPEG, HEIC, video thumbnails, Nikon NEF, DNG, and storage-provider thumbnail URLs still need their own adapters.
- CloudDrive API compatibility is tied to `clouddrive2-client` 0.3.0 and should be guarded by a startup capability probe.
- The current CLI asks CloudDrive for fresh file metadata before checking the local cache.
  This cost varied from 34 to 377 ms in two measured warm runs, and offline browsing will need a local-index-first path.
- Cache eviction is contract-tested with synthetic small budgets, while a long-running real 8 GiB workload still needs soak testing.

## Next decision

The key Phase 0 hypothesis is supported: a remote ARW can become visible without full materialization and then behave like a local cached asset.
The next useful slice is a read-only library scanner that imports one real date directory into SQLite, schedules bounded preview jobs, and exposes the resulting contact sheet to a minimal native macOS shell.
