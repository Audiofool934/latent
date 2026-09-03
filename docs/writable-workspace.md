# Writable workspace contract

Date: 2026-09-04

## Outcome

Latent 的用户创作状态已经与可重建 Library 索引物理分离。
默认 workspace 位于 `~/Library/Application Support/Latent/workspace/workspace.sqlite`。
删除或重建 `phase0/index.sqlite`、contact cache 或 embedding store 不会删除 Sequence。

第一版 store 支持：

- 创建、命名和注释 Sequence
- 把一个或多个档案资产加入 Sequence
- 保持每个 Sequence 内稳定且连续的照片顺序
- 完整重排和单项移除
- 原子 JSON 导出
- 非覆盖式 merge import

## Stable archive reference

Sequence item 不把 Library 的整数 asset ID 当作持久身份。
它保存 `provider + remote_path` 作为稳定档案引用，并记录加入时的 fingerprint。
为了在 Library 暂时不可用或重建期间仍可读，item 还保存文件名、拍摄时间、camera model 和 lens model 的最小快照。

同一个 Sequence 内的 `provider + remote_path` 必须唯一。
重复加入同一资产会返回 skipped，不会创建第二个 item。
指纹用于后续显示档案内容是否已经变化，不用于自动覆盖用户状态。

## Durability

Workspace SQLite 开启 foreign keys、WAL、`synchronous=FULL` 和 5 秒 busy timeout。
Sequence 和 item 写入使用事务。
并发加入时，position 读取与插入都在同一个 `BEGIN IMMEDIATE` 中完成，避免两个请求取得相同顺序位置。

重排要求客户端提交当前 Sequence 的完整 item ID 集合，而且每个 ID 恰好出现一次。
数据库先把旧 position 移到安全区，再写入从零开始的连续新顺序，因此不会触发中间态唯一键冲突。
删除一个 item 后，剩余 position 会在同一事务内重新压缩。

## Export and import

导出格式包含 format、schema version、exported time、Sequence metadata 和全部 item reference。
文件先写入同目录临时文件，执行 flush 与 fsync，再通过原子 replace 更新目标路径。
导出文件权限设为 `0600`。

Import 默认只执行 merge。
不存在的 Sequence ID 会完整导入。
完全相同的 Sequence 记为 unchanged。
相同 ID 但内容不同的 Sequence 记为 conflict 并跳过，绝不静默覆盖本地编辑。
导入会在任何写入前验证格式、schema、唯一 ID、唯一档案引用和从零连续的 item position。

可重复命令为：

```bash
uv run latent workspace-status --json
uv run latent workspace-export --output ./latent-workspace.json --json
uv run latent workspace-import --input ./latent-workspace.json --json
```

这些命令只读写本地 workspace 与用户明确指定的 export 文件。
它们不创建 CloudDrive client，不读取 RAW，不写入 aDrive，输出中的 `archive_modified` 始终为 false。

## Remaining integration

下一步是把 store 接入 loopback HTTP API 与网页 Sequence 界面。
Curator 生成的 source-first Sequence seed 会通过相同稳定档案引用批量加入，而不是复制或移动照片。
