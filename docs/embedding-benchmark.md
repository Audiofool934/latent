# Local image-text embedding benchmark

Date: 2026-09-02

## Decision

推荐使用 `google/siglip2-base-patch16-224` 作为 Latent 第一版全库视觉与语义索引模型。
这项推荐基于当前 M2 Pro 16 GB 机器、256 张真实 contact preview、100 个拍摄日期和 6 组中英文查询的本地实测。
全库任务仍然需要用户确认下面的资源预算后才能启动。

[MobileCLIP2](https://github.com/apple/ml-mobileclip) 是速度优先候选。
[SigLIP2](https://arxiv.org/abs/2502.14786) 是明确支持多语言理解与检索的质量优先候选，本轮使用 Google 发布的 [base patch16 224 checkpoint](https://huggingface.co/google/siglip2-base-patch16-224)。

## Safety boundary

基准只读取已经存在的本地 contact JPEG。
它没有读取 RAW，没有访问 CloudDrive，没有上传照片，也没有对 aDrive 或摄影档案执行写操作。
模型和结果保存在 `~/Library/Application Support/Latent/embedding-benchmark/`，与项目环境和可重建 Library 索引分离。

## Measured results

| Metric | MobileCLIP2-S2 | SigLIP2 base |
| --- | ---: | ---: |
| Embedding dimensions | 512 | 768 |
| Physical model cache | 397.7 MB | 1,535.2 MB |
| Warm image throughput, median of 3 | 57.1 images/s | 68.3 images/s |
| Projected pure encoding time for 25,793 images | 7.5 min | 6.3 min |
| Cached model load | 3.1 s | 9.7 s |
| Peak process RSS | 1,146 MiB | 948 MiB |
| MPS driver allocation after benchmark | 1,201 MiB | 2,539 MiB |
| Mean paired EN/ZH text cosine | 0.634 | 0.856 |
| Mean paired EN/ZH Top-12 overlap | 16.7% | 54.2% |

两套模型都使用 batch size 8，并在计时前预热一个 batch。
每套模型的吞吐量取三次完整 256 张编码的中位数。
纯编码推算不包括 SQLite checkpoint、向量写入和最终一致性检查，因此不是全库任务的承诺用时。

## Retrieval quality

测试查询覆盖雪景与桥、飞鸟、近景肖像、暗夜月亮、夜间城市灯光、山水六个主题。
每个主题各有英文和中文查询，结果不使用文件名作为语义依据。

SigLIP2 在飞鸟、月亮和夜景三组查询中，中英文 Top-12 重合率分别达到 66.7%、83.3% 和 75.0%。
视觉检查显示这三组的前排照片都能保持主体与场景一致。
肖像与山水的重合率较低，但 SigLIP2 的中文 Top-6 仍然保持人物或山水主题。

MobileCLIP2-S2 的英文结果可用，但中文查询经常漂移到路牌、建筑、合影或其他无关画面。
它在肖像查询的中英文 Top-12 没有重合，在飞鸟和夜景查询中都只有一张重合。
这个差异足以抵消 MobileCLIP2 更小模型体积的优势。

六组四行对照拼图保存在本地 benchmark results 目录。
每张拼图依次展示 MobileCLIP 英文、MobileCLIP 中文、SigLIP2 英文和 SigLIP2 中文的 Top-6。
原始 JSON 结果也保存在同一目录，但不提交到 Git，因为它包含真实文件名、拍摄时间和本地缓存路径。

## Upstream warning

Transformers 5.16.1 加载 SigLIP2 时会输出 `bos_token_id` 和 `eos_token_id` 的词表范围警告。
本地检查显示 SigLIP2 嵌套 text config 的真实词表大小为 256,000，processor 生成的两种语言 token 都在这个范围内。
模型的图像与文本前向计算、归一化和跨语言检索都成功完成。
这条警告目前没有表现为无效 embedding，但仍应作为上游兼容性信号保留在验证记录中。

## Full-index approval budget

选择 SigLIP2 后，全库任务的资源预算如下：

- 网络：0 字节照片网络读取，模型已经下载完成。
- 照片读取：只读取约 609 MB 的现有本地 contact cache。
- 运行时间：预计 8 到 12 分钟，包括编码、分批提交、checkpoint 和一致性检查。
- 内存：batch size 8 的实测 process RSS 为 948 MiB，MPS driver allocation 为 2,539 MiB，任务预算上限为约 4 GB unified memory。
- 模型磁盘：1,535.2 MB。
- 向量磁盘：25,793 个 768 维 float16 向量的原始大小约 37.8 MiB，连同 SQLite、journal、manifest 和原子导出余量，新增索引预算上限为 250 MB。
- 可恢复性：按小批次提交，已匹配 asset fingerprint 的 embedding 会跳过，中断后从缺失或过期项继续。
- 档案安全：不移动、重命名、覆盖或删除 RAW 与 sidecar，aDrive 保持零写入。

只有在用户确认这份预算后，才启动 25,793 张全库 embedding job。

## Implementation consequence

第一版索引应把 float16 vector 与 asset fingerprint 保存在可重建的独立 SQLite store 中。
25,793 个 768 维向量可以在查询时加载为约 75.6 MiB 的 float32 matrix，直接 cosine search 足够简单，也避免提前引入 ANN 索引的更新复杂度。
相似照片不需要再次运行图像模型。
自然语言搜索需要 SigLIP2 text encoder，服务可以在第一次语义查询时延迟加载并复用模型。

Curator 必须只根据向量邻近关系、拍摄时间和已有 EXIF 生成候选主题与关系依据。
它不能把视觉相似性描述成已知地点、人物身份或真实故事。
