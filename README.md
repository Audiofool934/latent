# Latent

> A personal photographic operating system for rediscovery, curation, and new work.

Latent 是一个面向个人摄影档案的 macOS-first 桌面应用。
它让沉睡在网络存储中的照片重新变得可浏览、可理解、可组织，并最终形成新的作品。

项目当前已完成 Phase 0，并进入 Phase 1 的 Library slice。
Python 数据层已经跑通 CloudDrive 只读元数据、HTTP Range、ARW 内嵌预览、SQLite 索引、分层缓存、日期目录扫描和可恢复任务队列，最终 GUI 技术栈仍保持开放。

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
uv run latent work --max-jobs 25
uv run latent status
uv run pytest
```

默认状态保存在 `~/Library/Application Support/Latent/phase0/`。
CloudDrive device token 只从应用自己的本地 plist 读取到内存，不会写入项目、SQLite、缓存文件或命令输出。
底层预览证据见 [Phase 0 results](docs/phase-0-results.md)。
目录扫描和任务队列证据见 [Phase 1 library slice](docs/phase-1-library-slice.md)。

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

### Phase 1: Library MVP

完成真实档案索引、contact sheet、inspector、搜索、Sequence 和 DxO handoff。
日期目录扫描和可恢复预览队列已经完成首轮真实验证，contact sheet 正在实现。

### Phase 2: Curator

加入视觉 embedding、相似照片、跨年份母题、关系解释和 Sequence 建议。

### Phase 3: Derivatives

从 Sequence 衍生网页、书稿、展览墙、短片分镜、年度回顾和可复用研究笔记。

## Open decisions

- 原生 SwiftUI、Tauri 或其他 macOS 桌面技术栈
- CloudDrive API 的稳定接入与凭证边界
- 8 GB 默认缓存是否需要按磁盘空间动态调整
- embedding 模型与本地推理性能
- Writable workspace 的备份与交换格式
- Sequence 第一版的交互粒度与导出目标
