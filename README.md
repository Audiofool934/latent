# Latent

> A personal photographic operating system for rediscovery, curation, and new work.

Latent 是一个面向个人摄影档案的 macOS-first 桌面应用。
它让沉睡在网络存储中的照片重新变得可浏览、可理解、可组织，并最终形成新的作品。

项目当前已完成 Phase 0，并进入 Phase 1 的 Library slice。
Python 数据层已经跑通 CloudDrive 只读元数据、HTTP Range、ARW 内嵌预览、SQLite 索引、分层缓存、可恢复目录树扫描和预览任务队列。
本地只读 contact sheet 已可从 SQLite 与缓存直接运行，最终桌面 GUI 技术栈仍保持开放。
首轮真实全库任务已经为 25,793 张 ARW 生成 contact preview，当前队列没有 pending、running 或 failed 项。
高容量日期按 250 张一批增量加载，不受单页上限影响。
本地图文 embedding 小样本基准已经完成，SigLIP2 base 是当前的中英文语义检索模型。
跨日期语义搜索、相似照片 API 和对应界面已经在 3 张真实 contact preview 的隔离向量库上跑通。
第一版 grounded Curator 已能把相似度、拍摄日期和一致的 EXIF 汇成可追溯观察与 Sequence seed，不生成地点、身份或故事。
独立 writable workspace 已具备持久化 Sequence、稳定档案引用、顺序修改、网页交互和非覆盖式导入导出契约。
正式图库仍保持 25,793 个 pending、0 个 vector，全库索引等待资源预算确认。

## Phase 0 spike

当前代码只验证底层数据链路，不代表最终桌面应用会使用 Python 构建界面。
CloudDrive 适配器与预览管线保持隔离，因此后续可以由 SwiftUI、Tauri 或其他 GUI 调用同一契约。

本机准备：

- CloudDrive 正在运行，且本地 Cloud API 可用
- `exiftool`
- `uv`

```bash
uv sync
uv run latent spike --path '/CloudName/path/to/photo.ARW'
uv run latent scan --path '/CloudName/path/to/YYYY-MM-DD'
uv run latent scan-tree --path '/CloudName/archive-root' --max-directories 25
uv run latent scan-tree --scan-id 1 --max-directories 100
uv run latent scan-tree-cancel --scan-id 1
uv run latent work --max-jobs 25
uv run latent status
uv run latent embedding-status
uv run latent embedding-sync
uv run latent workspace-status
uv run latent workspace-export --output ./latent-workspace.json
uv run latent workspace-import --input ./latent-workspace.json
uv run latent serve
uv run pytest
```

语义查询需要先用 `uv sync --group embedding-bench` 安装本地 SigLIP2 runtime。
服务只从已经下载的模型目录加载，不会在查询时联网下载模型。

默认状态保存在 `~/Library/Application Support/Latent/phase0/`。
CloudDrive device token 只从应用自己的本地 plist 读取到内存，不会写入项目、SQLite、缓存文件或命令输出。
底层预览证据见 [Phase 0 results](docs/phase-0-results.md)。
目录扫描和任务队列证据见 [Phase 1 library slice](docs/phase-1-library-slice.md)。
本地图文模型的质量、性能与资源证据见 [embedding benchmark](docs/embedding-benchmark.md)。
用户创作状态的持久化与交换契约见 [writable workspace](docs/writable-workspace.md)。
`latent serve` 默认只监听 `127.0.0.1:8765`，并且服务进程没有 CloudDrive 客户端，因此浏览界面只会读取本地索引与派生缓存。
`scan-tree` 默认跳过名称以 `.` 或 `_` 开头的辅助目录，避免把修复区、元数据和导出文件混入正常图库。
只有明确传入 `--include-hidden` 才会遍历这些目录。
`work` 在启动时按目录中的拍摄日期重新排列 pending job，较新的日期优先。
连续五次 provider 失败会触发熔断，`Ctrl-C` 会把当前 job 安全放回 pending。

## Why Latent

照片已经安全地进入长期档案，但“存下来”不等于“用起来”。
当原始素材位于 aDrive 这类按需网络存储中，Finder Quick Look 的等待、跨年份目录的割裂，以及 RAW 文件的体积，会让回看与整理逐渐变得昂贵。

Latent 解决的是档案被动沉睡的问题。
名字同时指向暗房中尚未显影的 latent image，以及模型用来发现视觉关系的 latent space。

## Product thesis

Latent 不是另一个备份客户端、通用相册管理器或 RAW 编辑器。
它是一层建立在可信摄影档案之上的个人工作空间：

1. 快速看见整个档案，而不必先下载原片。
2. 通过时间、地点、器材、视觉母题和自然语言重新发现照片。
3. 让 AI 以策展人与研究伙伴的方式揭示有依据的联系。
4. 把发现组织成 Sequence，并继续交给 DxO、Finder 或导出流程。

## Experience

主界面采用三栏 GUI：

- 左侧是 Library、时间线、筛选器和已保存的 Sequence。
- 中央是以照片为主角的 contact sheet、单图预览和自由编排画布。
- 右侧是 EXIF、来源、关联照片、策展解释和当前 Sequence。

视觉语言来自深色 TUI：克制、精确、信息密度高。
键盘不是另一套界面，而是 GUI 的加速层，包括 command palette、全局搜索、快速评分与加入 Sequence。

AI 不以聊天窗口作为主要形态。
它嵌入搜索结果、关系解释、主题聚类和 Sequence 建议中，并且每个判断都能回到具体照片与元数据。

## Core loop

1. **Discover**：按日期、地点、相机、镜头、评分、颜色、主体或自然语言检索。
2. **Understand**：查看照片之间的时间、视觉与语义关系，以及关系成立的依据。
3. **Sequence**：把照片编排成一个可命名、可注释、可迭代的视觉序列。
4. **Act**：在 DxO 中打开 RAW、在 Finder 中定位、导出选片或生成派生作品。

## Data architecture

Latent 将数据明确分成三层：

### 1. Immutable archive

原始 RAW、JPEG、视频和 DxO `.dop` 等 sidecar 保留在 aDrive 摄影档案中。
浏览、搜索和策展默认只读，不在后台移动、重命名或覆盖原始素材。

### 2. Rebuildable local index

本地保存可重新生成的数据，包括：

- 文件路径、尺寸、哈希和可用性状态
- EXIF 与拍摄时间
- 缩略图与中等尺寸预览
- 视觉 embedding、聚类与搜索索引

这一层可以删除后从档案重建，不承担唯一数据源的职责。

### 3. Writable workspace

本地工作区保存用户真正创造的状态，包括：

- 评分、标签与笔记
- 已保存的搜索
- Sequence 与照片顺序
- 策展解释和派生输出记录

这一层需要独立备份，并保持可导出、可迁移。

## Network-aware preview

Latent 不依赖 Finder/macFUSE 的整文件物化来完成日常浏览。
首选路径是直接使用 CloudDrive API 获取元数据与字节范围：

- RAW 优先读取文件内部嵌入的 JPEG preview，不下载整张 RAW。
- JPEG 与视频优先使用存储服务提供的 thumbnail 或 preview URL。
- 完整原片仅在用户明确打开、编辑或导出时获取。

CloudDrive 1.0.16 在 macOS 上提供按需流式读取大文件的能力，但它与 Latent 的批量索引路径承担不同职责。
批量 contact sheet 与 inspector 继续使用可验证的 HTTP Range，并在服务端忽略 Range 时拒绝读取完整响应。
用户明确把原片交给 DxO 时，可以把 macFUSE 挂载路径作为候选 handoff 通道，使编辑器按自己的访问模式读取文件。
Finder 双击或“打开方式”仍可能先要求完整下载，因此不能被 Latent 当作流式 handoff。
直接 handoff 还需要用一张未缓存 RAW 做端到端验证，确认具体启动方式不会回退到 Finder 的完整物化路径。
已经读取的原片区段会占用 CloudDrive 本地缓存，这部分空间与 Latent 自己的派生预览缓存相互独立。

初始缓存方案为可配置的 8 GB 上限：

- 约 3 GB 常驻 contact thumbnails
- 约 4 GB 中等尺寸预览 LRU
- 约 1 GB 高分辨率临时缓存

首轮索引优先处理最近日期，并在可暂停、可恢复的后台任务中逐步补齐历史缩略图和 embedding。
未来从存储卡导入时，应在上传前生成预览，使新照片进入档案后立即可用。

## MVP

第一版只证明一件事：网络档案可以像本地照片库一样被快速重新使用。

MVP 包含：

1. 不完整下载 RAW 即可建立文件、EXIF、哈希与预览索引。
2. 可流畅浏览的 contact sheet 与单图 inspector。
3. 元数据筛选和自然语言检索。
4. Sequence 创建、排序、命名与注释。
5. 将选中的原始文件交给 DxO，或在 Finder 中定位。

归档健康度、导入队列和缺失文件检查属于辅助界面，不占据首页中心。

## Safety contract

- 保留每一张 RAW，除非用户针对明确文件给出删除授权。
- 拍摄日期以相机写入的 `EXIF:DateTimeOriginal` 为准。
- 同名冲突必须保留双方，不静默覆盖。
- DxO sidecar 与对应 RAW 一起追踪。
- 任何会修改远端档案的能力都必须显式触发、可预览并留下记录。
- 缓存和索引损坏不能影响原始档案与工作区数据。

## Non-goals

- 替代 aDrive 或承担云备份职责
- 替代 DxO PhotoLab 的 RAW 调色与降噪
- 自动整理或重写远端目录结构
- 以聊天机器人包装一个普通文件浏览器
- 在缺乏出处时为照片编造地点、人物或故事

## Initial success targets

- 已缓存的 contact sheet 在本地即时出现。
- 普通网络状态下，未缓存 RAW 的嵌入预览目标在约 1.5 秒内可见。
- 浏览与检索不会触发完整 RAW 的批量下载。
- 用户能在数万张照片中，从一次搜索完成一个 Sequence，并无缝进入 DxO。
- 在只读模式下运行时，远端摄影档案保持零修改。

## Delivery plan

### Phase 0: Read-only spike

验证 CloudDrive API、范围读取、ARW 内嵌预览提取、SQLite 索引和缓存淘汰策略。
核心纵向链路已在三张真实 ARW 上跑通，包括 A7R II、A7R V、横幅和竖幅样本。

### Phase 1: Library and discovery MVP

完成真实档案索引、contact sheet、inspector、SigLIP2 embedding、跨日期语义搜索、相似照片、受约束 Curator、Sequence 和 DxO handoff。
日期目录扫描、可恢复预览队列和本地只读 contact sheet 已经完成首轮真实验证。
完整摄影档案的元数据遍历已发现 25,793 个 ARW 条目，首轮预览任务已经全部成功完成。
日期分页已在包含 2,656 张照片的真实日期上完成端到端验证，并能到达最后一页。
embedding queue、语义搜索、相似照片、grounded Curator 和 Sequence 链路已经完成有界验证，全库向量构建仍等待资源确认。

### Phase 2: Curator expansion

在第一版有依据的关系解释之上，加入主题聚类、跨年份母题、可比较的策展方向和更完整的 Sequence 建议。

### Phase 3: Derivatives

从 Sequence 衍生网页、书稿、展览墙、短片分镜、年度回顾和可复用研究笔记。

## Open decisions

- 原生 SwiftUI、Tauri 或其他 macOS 桌面技术栈
- CloudDrive API 的稳定接入与凭证边界
- 8 GB 默认缓存是否需要按磁盘空间动态调整
- 全库 embedding 的调度与可接受资源窗口
- Curator 输出的保存、重算与版本边界
- Writable workspace 的自动备份频率与目标位置
- Sequence 的衍生导出目标与跨设备同步边界
