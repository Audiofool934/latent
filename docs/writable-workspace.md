# Writable workspace contract

Updated: 2026-10-04

## Outcome

Latent 的用户创作状态已经与可重建 Library 索引物理分离。
默认 workspace 位于 `~/Library/Application Support/Latent/workspace/workspace.sqlite`。
删除或重建 `phase0/index.sqlite`、contact cache 或 embedding store 不会删除 Sequence。

第一版 store 支持：

- 创建、命名和注释 Sequence
- 用户自定义的多层文件夹分类，以及跨层移动
- 把一个或多个档案资产加入 Sequence
- 保持每个 Sequence 内稳定且连续的照片顺序
- 完整重排和单项移除
- 原子 JSON 导出
- 非覆盖式 merge import
- 独立持久化星级、caption，以及 `unmarked`、`pick`、`reject` 三种标记

Schema 3 在现有 `photo_annotations` 表中增加 `flag` 字段。
旧记录默认 `unmarked`，原有星级、caption 和 Sequence 保持不变。
标记、星级与 caption 按字段独立更新，Reject 不删除原图或 workspace 引用。
导出包含标记，导入继续兼容 schema 1、2、3、4，旧导入文件缺少标记时默认为 `unmarked`。

## Sequence folder hierarchy

Schema 4 新增 `sequence_folders`，通过 `parent_id` 保存父子关系，并为 `sequences` 增加可空的 `folder_id`。
空父级表示根目录，已有 Sequence 升级后保留在根目录。
文件夹名称由用户决定，名称可以重复，UUID 用于区分节点。
创建和移动都会验证父级存在，移动在 `BEGIN IMMEDIATE` 事务内检查祖先链，禁止自身或后代成为父级。
删除文件夹会在同一个事务中把直接子文件夹与 Sequence 提升到原父级，不删除 Sequence 或照片引用。
层级深度不设固定上限；遍历和导入排序使用迭代方式，并验证无环关系。
导出包含平面的文件夹列表及父级引用，导入先验证完整树和 Sequence 所属关系，再按父级先于子级的顺序写入。
已存在且内容不同的文件夹 ID 保持本地内容，使用与 Sequence 相同的非覆盖导入原则。

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

导出格式包含 format、schema version、exported time、文件夹层级、Sequence metadata 和全部 item reference。
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

## HTTP and interface evidence

Loopback 服务已经提供 Sequence list、detail、create、update、add items、reorder、remove item 和 delete API。
`GET /api/sequences` 同时返回文件夹列表。
`POST /api/sequence-folders` 创建文件夹，`PATCH` 支持独立改名或移动，`DELETE` 执行保留内容的文件夹移除。
Sequence 的 `PATCH` 接受 `folder_id`，显式传入 `null` 表示移动到根目录。
Workspace mutation 都要求 `application/json`，body 上限为 64 KiB。
服务绑定非 loopback 地址时，即使用户显式允许远程浏览，也会禁用 workspace mutation。

网页已完成以下真实手势路径：

- 在 `1440x900` 通过对话框创建名为 `Field Motion` 的 Sequence，并保存 note
- 从日期 contact sheet 连续加入 2 张照片，sidebar 和 active selector 的 item count 同步为 2
- 在中央打开 Sequence，把第一张移动到第二位，并保持选中资产不变
- 把 Sequence 改名为 `Field Motion Study` 并更新 note
- 从 grounded Curator 把 3 张 seed 一次加入，item count 从 2 变为 5
- 再次加入相同 seed 时返回 3 already present，item count 保持 5
- 在 `390x844` 显示 5 张两列 Sequence contact sheet，并从 inspector 使用顺序控制
- 刷新带 `sequence` 参数的 URL 后仍恢复同一 Sequence、5 张顺序和 active selector
- 关闭服务并从同一个 workspace 启动全新服务实例后，跨 2024、2025、2026 的 3 张测试 Sequence 仍保留名称、备注、顺序和 current Library 状态
- 从全局视觉 motif 创建跨年 Sequence、加入 seed、重复加入去重并再次重启服务后，2 张真实 Library 引用仍保持不变

桌面和手机状态都没有页面横向溢出，浏览器控制台没有 warning 或 error。
验收使用隔离的临时 workspace，没有写入用户的正式 workspace。

Curator seed 通过同一稳定档案引用批量加入。
这个操作只保存引用和顺序，不复制、移动、下载或改名照片。

## Remaining decisions

JSON export/import 已可通过 CLI 使用，但尚未放进网页设置界面。
自动备份频率、目标位置、跨设备同步和面向 DxO 或网页作品的衍生导出仍需单独设计。
