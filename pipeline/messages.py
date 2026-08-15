"""
日志消息集中管理
所有模块的日志格式统一在此定义，修改一处全局生效
"""


class Msg:
    """日志消息模板"""

    # ── 输入 ──
    INPUT_READ = "读取 {count} 个镜头: {file}"
    INPUT_SHOT = "  镜号 {id}: {dur}s | {desc}"

    # ── Agent ──
    AGENT_START = "处理 {count} 个镜头..."
    AGENT_SHOT = "镜号 {id}: {wf_id} | {prompt}"
    AGENT_DIST = "模板分布: {dist}"

    # ── ComfyUI ──
    GEN_INJECTABLE = "发现 {count} 个可注入节点"
    GEN_NODE = "  节点 {nid} ({ct}): {params}"
    GEN_UPLOAD = "上传: {name} -> {server}"
    GEN_SUBMIT = "提交: {pid}"
    GEN_SUBMIT_BATCH = "并发提交 {count} 个任务..."
    GEN_SUBMIT_OK = "成功提交 {done}/{total} 个任务"
    GEN_SUBMIT_NONE = "没有成功提交的任务"
    GEN_POLL = "开始并发轮询..."
    GEN_DONE = "完成: {pid} ({elapsed}s)"
    GEN_STATUS = "{pid} 状态: {status}"
    GEN_DOWNLOAD = "下载: {path}"
    GEN_DONE_SUMMARY = "完成: {done}, 失败: {failed}, 待后期(FFmpeg): {pending}"
    GEN_DONE_SUMMARY2 = "完成: {done}/{total}, 失败: {failed}"
    GEN_SKIP = "非 ComfyUI，跳过"
    GEN_FFMPEG = "镜号 {id}: 模板 {tid} (FFmpeg，需后期处理)"

    # ── ComfyUI 错误 ──
    GEN_ERR_UPLOAD = "镜号 {id} 上传失败: {err}"
    GEN_ERR_SUBMIT = "镜号 {id} 提交失败: {err}"
    GEN_ERR_POLL = "镜号 {id} 失败: {err}"
    GEN_ERR_EXEC = "失败: {msg}"
    GEN_ERR_VALIDATE = "验证错误:"
    GEN_ERR_NODE = "  节点 {nid}: {err}"

    # ── 筛选 ──
    FILTER_PASS = "镜号 {id}: {dur}s | {res} | {size} MB"
    FILTER_SUMMARY = "通过: {passed}/{total}"

    # ── 合并 ──
    MERGE_START = "镜头合并（先拼接 → 后字幕语音）"
    MERGE_TTS = "TTS: {text}"
    MERGE_TTS_OK = "TTS 完成: {file}"
    MERGE_TTS_GLOBAL = "全局 TTS（{chars} 字），时间轴为整个合并视频..."
    MERGE_NO_DIALOGUE = "所有镜头无台词，跳过 TTS"
    MERGE_NORMALIZE = "补静音音轨: {file}"
    MERGE_CONCAT = "拼接 {count} 段视频..."
    MERGE_MERGED = "合并视频: {path}"
    MERGE_CONCAT_FALLBACK = "concat 失败，改用重新编码拼接..."
    MERGE_SKIP = "跳过失败的镜头: {id}"
    MERGE_DURATION = "{file}: {dur}s"
    MERGE_TOTAL = "总时长: {dur}s"
    MERGE_TOTAL_COUNT = "共 {count} 段，开始拼接..."
    MERGE_PAD_VIDEO = "语音比视频长，视频末帧补帧: +{dur}s"
    MERGE_PAD_AUDIO = "语音比视频短，音频补静音: +{dur}s"
    MERGE_FINAL = "最终合成（替换原声 + 全局字幕）: {file}"

    # ── 合并错误 ──
    MERGE_ERR_TTS = "TTS 失败: {err}"
    MERGE_ERR_FINAL = "最终合成失败: {err}"

    # ── LangGraph 节点 ──
    LG_ANALYZE = "场景包含: {elements}"
    LG_SELECT = "{wid} ({wtype})"
    LG_SELECT_FALLBACK = "无 registry，默认 seedance_i2v"
    LG_OPTIMIZE = "{prompt}"
    LG_VALIDATE_OK = "✅"
    LG_VALIDATE_FAIL = "❌ {msg}"
    LG_SUBMIT_SKIP = "非 ComfyUI，跳过"
    LG_SUBMIT_OK = "提交 {pid}"
    LG_WAIT_OK = "完成 {path}"
    LG_WAIT_FAIL = "❌ {err}"
    LG_REVIEW_PASS = "✅ pass"
    LG_REVIEW_SKIP = "跳过（状态={status}）"
    LG_BATCH_START = "处理 {count} 个镜头..."
    LG_BATCH_DONE = "完成: {done}, 失败: {failed}, 待FFmpeg: {ffmpeg}"

    # ── Pipeline marker 行（stdout 上传给 web 后端解析） ──
    PIPE_EVENT_PREFIX = "@@PIPE_EVENT@@"

    # ── Registry ──
    REG_REGISTERED = "已注册: {tid} - {name}"
    REG_DIR_MISSING = "模板目录不存在: {dir}"
    REG_LOAD_FAIL = "加载失败 {file}: {err}"

    # ── main 流程标题 ──
    MAIN_INPUT = "═══ 脚本输入 ═══"
    MAIN_AGENT = "═══ Agent 选模板 + 改 Prompt ═══"
    MAIN_GEN = "═══ ComfyUI 并发生成 ═══"
    MAIN_FILTER = "═══ 筛选 ═══"
    MAIN_MERGE = "═══ 镜头合并 + 音频字幕 ═══"
    MAIN_FINAL = "最终视频: {path}"
    MAIN_SKIP_GEN = "跳过生成，扫描已有视频..."
    MAIN_SKIP_MERGE = "跳过合并"
    MAIN_RESTORE = "恢复 {count} 个视频"
    MAIN_STATUS = "镜号 {id}: {status}"