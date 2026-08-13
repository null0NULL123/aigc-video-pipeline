"""
ComfyUI 生成（异步并发版）
aiohttp 批量提交 → 并发轮询等待
"""
import json
import asyncio
import aiohttp
from pathlib import Path

from pipeline.log import get_logger
from pipeline.messages import Msg
log = get_logger("comfyui")


class ComfyUIClient:
    """ComfyUI API 客户端（异步）"""

    def __init__(self, host: str = "http://127.0.0.1:8188",
                 timeout: int = 600, poll_interval: int = 5):
        self.host = host.rstrip("/")
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.workflow = None
        self.session = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()

    def load_workflow(self, workflow_path: str):
        """加载 API 格式工作流"""
        path = Path(workflow_path)
        if not path.exists():
            raise FileNotFoundError(f"工作流文件不存在: {workflow_path}")

        with open(path, encoding="utf-8") as f:
            raw = json.load(f)

        if "nodes" in raw and "links" in raw:
            raise ValueError(
                "检测到 Editor 格式！请在 ComfyUI 中 "
                "Workflow → Export (API) 导出 API 格式"
            )
        if "prompt" in raw:
            self.workflow = raw["prompt"]
        else:
            self.workflow = raw

        self._find_injectable_nodes()

    def _find_injectable_nodes(self):
        """扫描可注入参数的节点"""
        self.injectable = {}
        for nid, node in self.workflow.items():
            ct = node.get("class_type", "")
            inputs = node.get("inputs", {})
            params = {}

            # Prompt — 覆盖各种 I2V/T2V 节点
            if ct == "CLIPTextEncode" and "text" in inputs:
                params["prompt"] = ("text", str)
            elif "JimengSeedance" in ct and "prompt" in inputs:
                params["prompt"] = ("prompt", str)
                params["duration"] = ("duration", int)
                params["seed"] = ("seed", int)
                params["filename_prefix"] = ("filename_prefix", str)
            elif "MiniMaxH3ImageToVideo" in ct and "prompt" in inputs:
                params["prompt"] = ("prompt", str)
            elif "WanVideoI2V" in ct and "prompt" in inputs:
                params["prompt"] = ("prompt", str)

            # Seed / Steps
            if "KSampler" in ct or "SamplerCustom" in ct:
                if "seed" in inputs:
                    params["seed"] = ("seed", int)
                if "steps" in inputs:
                    params["steps"] = ("steps", int)

            # Latent 尺寸/帧数
            if ct in ("EmptyLatentImage", "EmptySD3LatentImage",
                      "EmptyHunyuanLatentVideo", "EmptyMochiLatentVideo",
                      "EmptyLTXVLatentVideo"):
                for k in ("width", "height"):
                    if k in inputs:
                        params[k] = (k, int)
                lk = "length" if "length" in inputs else \
                     "batch_size" if "batch_size" in inputs else None
                if lk:
                    params["frame_count"] = (lk, int)

            # 图片
            if ct == "LoadImage" and "image" in inputs:
                params["image"] = ("image", str)

            if ct == "VHS_VideoCombine" and "frame_rate" in inputs:
                params["fps"] = ("frame_rate", int)

            if params:
                self.injectable[nid] = {"class_type": ct, "params": params}

        log.info(Msg.GEN_INJECTABLE.format(count=len(self.injectable)))
        for nid, info in self.injectable.items():
            log.info(Msg.GEN_NODE.format(nid=nid, ct=info["class_type"], params=list(info["params"].keys())))

    def inject_params(self, task: dict) -> dict:
        """注入任务参数到工作流副本"""
        wf = json.loads(json.dumps(self.workflow))
        for nid, info in self.injectable.items():
            for param_key, (input_key, _) in info["params"].items():
                if param_key in task and task[param_key] is not None:
                    existing = wf[nid]["inputs"].get(input_key)
                    if isinstance(existing, list) and len(existing) == 2:
                        continue
                    wf[nid]["inputs"][input_key] = task[param_key]
        return wf

    # ---------- 单步 API 调用 ----------

    async def upload_image(self, image_path: str) -> str:
        """上传图片，返回服务端文件名"""
        path = Path(image_path)
        if not path.exists():
            raise FileNotFoundError(f"图片不存在: {image_path}")

        data = aiohttp.FormData()
        data.add_field("image", open(path, "rb"), filename=path.name,
                       content_type="application/octet-stream")
        data.add_field("type", "input")
        data.add_field("overwrite", "true")

        async with self.session.post(
            f"{self.host}/upload/image", data=data
        ) as resp:
            resp.raise_for_status()
            result = await resp.json()
            log.info(Msg.GEN_UPLOAD.format(name=path.name, server=result["name"]))
            return result["name"]

    async def submit(self, workflow: dict) -> str:
        """提交工作流，返回 prompt_id"""
        async with self.session.post(
            f"{self.host}/prompt", json={"prompt": workflow}
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            pid = data["prompt_id"]

            if data.get("node_errors"):
                log.warning(Msg.GEN_ERR_VALIDATE)
                for nid, err in data["node_errors"].items():
                    log.info(f"       节点 {nid}: {err}")

            log.info(Msg.GEN_SUBMIT.format(pid=pid))
            return pid

    async def wait_for_completion(self, prompt_id: str) -> dict:
        """轮询单个任务直到完成，返回 history"""
        start = asyncio.get_event_loop().time()
        last_status = ""

        while True:
            elapsed = asyncio.get_event_loop().time() - start
            if elapsed > self.timeout:
                raise TimeoutError(f"超时 ({self.timeout}s): {prompt_id}")

            async with self.session.get(
                f"{self.host}/history/{prompt_id}"
            ) as resp:
                if resp.status != 200:
                    await asyncio.sleep(self.poll_interval)
                    continue

                data = (await resp.json()).get(prompt_id, {})
                status = data.get("status", {})
                status_str = status.get("status_str", "")
                completed = status.get("completed", False)

                if completed and status_str == "success":
                    elapsed = asyncio.get_event_loop().time() - start
                    log.info(Msg.GEN_DONE.format(pid=prompt_id, elapsed=f"{elapsed:.1f}"))
                    return data

                if status_str == "error":
                    msgs = status.get("messages", [])
                    for msg in msgs:
                        if msg[0] == "execution_error":
                            err = msg[1]
                            log.error(Msg.GEN_ERR_EXEC.format(msg=err.get("exception_message", "未知")))
                    raise RuntimeError(f"任务失败: {prompt_id}")

                cur = status_str or "pending"
                if cur != last_status:
                    log.info(Msg.GEN_STATUS.format(pid=prompt_id[:8], status=cur))
                    last_status = cur

            await asyncio.sleep(self.poll_interval)

    async def download_output(self, history: dict, output_dir: str,
                              shot_id: str) -> str:
        """下载输出到本地"""
        outputs = history.get("outputs", {})
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        for node_id, node_out in outputs.items():
            for key in ("video", "videos", "gifs", "images"):
                files = node_out.get(key, [])
                if not files:
                    continue
                for f_info in files:
                    filename = f_info.get("filename", "")
                    if not filename:
                        continue
                    subfolder = f_info.get("subfolder", "")
                    ftype = f_info.get("type", "output")

                    async with self.session.get(
                        f"{self.host}/view",
                        params={"filename": filename, "subfolder": subfolder,
                                "type": ftype}
                    ) as resp:
                        local = out_dir / f"shot_{shot_id}.mp4"
                        with open(local, "wb") as f:
                            f.write(await resp.read())
                        log.info(Msg.GEN_DOWNLOAD.format(path=str(local)))
                        return str(local)

        raise RuntimeError("history 中未找到输出文件")

    # ---------- 单任务 ----------

    async def generate_one(self, task: dict, output_dir: str,
                           image_path: str = None) -> str:
        """完整生成一个镜头"""
        task = {**task}

        # 上传图片
        if image_path and Path(image_path).exists():
            server_fn = await self.upload_image(image_path)
            task["image"] = server_fn

        # 注入 + 提交
        workflow = self.inject_params(task)
        pid = await self.submit(workflow)

        # 等待 + 下载
        history = await self.wait_for_completion(pid)
        return await self.download_output(history, output_dir, task["shot_id"])

    # ---------- 批量并发 ----------

    async def generate_batch(self, tasks: list[dict],
                             output_dir: str) -> list[dict]:
        """
        批量并发生成：
        1. 先全部提交（ComfyUI 会排队）
        2. 并发轮询所有任务
        """
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        results = []

        # ===== 阶段1: 全部提交 =====
        log.info(Msg.GEN_SUBMIT_BATCH.format(count=len(tasks)))
        submitted = []

        for task in tasks:
            task_copy = {**task}

            # 上传图片
            if task_copy.get("asset_type") in ("image", "local") and \
               task_copy.get("asset_path"):
                try:
                    server_fn = await self.upload_image(task_copy["asset_path"])
                    task_copy["image"] = server_fn
                except Exception as e:
                    log.error(Msg.GEN_ERR_UPLOAD.format(id=task_copy["shot_id"], err=e))
                    results.append({
                        "shot_id": task_copy["shot_id"],
                        "status": "failed",
                        "error": f"上传失败: {e}",
                    })
                    continue

            # 注入 + 提交
            try:
                workflow = self.inject_params(task_copy)
                pid = await self.submit(workflow)
                submitted.append({
                    "shot_id": task_copy["shot_id"],
                    "prompt_id": pid,
                    "task": task_copy,
                })
            except Exception as e:
                log.error(Msg.GEN_ERR_SUBMIT.format(id=task_copy["shot_id"], err=e))
                results.append({
                    "shot_id": task_copy["shot_id"],
                    "status": "failed",
                    "error": f"提交失败: {e}",
                })

        if not submitted:
            log.warning(Msg.GEN_SUBMIT_NONE)
            return results

        log.info(Msg.GEN_SUBMIT_OK.format(done=len(submitted), total=len(tasks)))
        log.info(Msg.GEN_POLL)

        # ===== 阶段2: 并发轮询 =====
        async def poll_one(item: dict) -> dict:
            shot_id = item["shot_id"]
            pid = item["prompt_id"]
            try:
                history = await self.wait_for_completion(pid)
                video_path = await self.download_output(
                    history, output_dir, shot_id
                )
                return {
                    "shot_id": shot_id,
                    "status": "done",
                    "video_path": video_path,
                }
            except Exception as e:
                log.error(Msg.GEN_ERR_POLL.format(id=shot_id, err=e))
                return {
                    "shot_id": shot_id,
                    "status": "failed",
                    "error": str(e),
                }

        poll_results = await asyncio.gather(
            *[poll_one(item) for item in submitted],
            return_exceptions=False,
        )
        results.extend(poll_results)

        # 统计
        done = sum(1 for r in results if r["status"] == "done")
        failed = sum(1 for r in results if r["status"] == "failed")
        log.info(Msg.GEN_DONE_SUMMARY2.format(done=done, total=len(tasks), failed=failed))
        return results