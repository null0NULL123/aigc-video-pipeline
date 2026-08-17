# aigc-video-pipeline

AIGC 视频批生产管道。读取分镜表 → ComfyUI / Seedream / Seedance 生成图视频 → edge-tts 配音 → FFmpeg 合并、加字幕、出成片。

## 功能

- **多 Provider 图视频生成**：ComfyUI 本地工作流 + 火山 Seedream（文生图）+ Seedance（图生视频 / 文生视频）
- **批量异步调度**：基于 aiohttp 的并发管线，单批可处理 N 个分镜
- **TTS 配音**：微软 edge-tts，句级停顿 + 静音探测
- **FFmpeg 合并**：转场、字幕烧录、H.264/AAC 编码
- **Web 管理面板**（FastAPI）：表格管理、批量任务、素材库、配置、系统状态

## 项目结构

```
.
├── app.py               # FastAPI 入口（Web 管理面板）
├── cli.py               # CLI 入口（异步批处理）
├── config.example.yaml  # 配置文件模板（cp 成 config.yaml 后填 API key）
├── pipeline/
│   ├── input_reader.py  # 读取 CSV / Excel 分镜表
│   ├── generator.py     # 异步批量调度
│   ├── providers/         # 生成后端客户端
│   │   ├── comfyui.py     # ComfyUI HTTP 客户端（兜底）
│   │   └── volcano.py     # 火山方舟 Ark API 直连（Seedream / Seedance，无需 ComfyUI）
│   ├── llm.py           # LLM 调用（生成 prompt）
│   ├── merge.py         # FFmpeg 合并 / 字幕
│   ├── registry.py      # 模板注册
│   └── messages.py      # 内部消息总线
├── templates/           # 各 provider 的 prompt / workflow 模板（JSON）
├── web/
│   ├── routers/         # FastAPI 路由（config / tables / pipeline / videos / media / assets / system）
│   └── static/          # 前端 HTML
├── test/                # pytest 测试套件（171+ 用例）
└── input/               # 分镜表与示例素材
```

## 功能

### 核心链路

```
CSV/Excel → 分镜表读取 → Seedream 出图 / Seedance 视频 / ComfyUI 工作流
                                    ↓
                           LLM 翻译 prompt + 润色对白
                                    ↓
                           批量并发生成（限流）
                                    ↓
                           镜头预览 & 人工筛选
                                    ↓
                           TTS 配音 + 字幕烧录
                                    ↓
                           FFmpeg 合并 → 成片
```

### 镜头预览与重做（SSE 实时进度）

生成完成后，每个镜头独立状态可查、可预览、可确认、可重做：

- **实时进度**：浏览器 SSE 推送，无需手动刷新
- **预览**：点预览直接播放镜头视频
- **确认**：逐镜头确认（或批量确认），只合并已确认的镜头
- **重做**：失败镜头可单独重做，不用整批重来
- **选择性合并**：只合并确认的镜头，其他跳过

工作流：`生成 → 逐镜头预览 → 确认/重做 → 合并已确认`

## 快速开始

```bash
# 1. 安装依赖（推荐 uv）
uv venv .venv --python 3.11
source .venv/bin/activate
uv pip install -r requirements.txt

# 2. 复制配置并填 API key
cp config.example.yaml config.yaml
# 编辑 config.yaml：填 jimeng.api_key / llm.api_key / comfyui.host

# 3. 跑测试
python -m pytest test/

# 4. 启动 Web 管理面板
python -m uvicorn app:app --host 127.0.0.1 --port 8000 --reload
# 或跑 CLI
python cli.py --config config.yaml --input input/demo_t2i.csv
```

## 配置说明

详见 `config.example.yaml`，关键项：

- `comfyui.host`：ComfyUI 服务地址，默认 `http://127.0.0.1:8188`
- `api.jimeng.api_key`：火山方舟 API key
- `llm.api_url / api_key`：可选 LLM（用于自动生成 prompt）
- `video.fps / width / height`：输出视频参数
- `tts.voice`：edge-tts 音色，默认 `zh-CN-XiaoxiaoNeural`
- `merge.transition`：分镜转场，默认 `fade`

## 输入格式

CSV 或 Excel 分镜表，至少包含：

| shot_id | prompt | duration | asset_type |
|---------|--------|----------|------------|
| 1       | ...    | 5        | ai_generated |

`asset_type` 可选：`ai_generated` / `uploaded`。`prompt` 太短会自动补长，太长会自动拆段。

详见 `input/demo_*.csv`。

## License

MIT