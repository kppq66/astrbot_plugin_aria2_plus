
import asyncio
import os

import requests

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register


@register(
    "aria2_plus",
    "pkq",
    "AstrBot 4.25 aria2增强下载助手",
    "2.4.0"
)
class Aria2Plus(Star):

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)

        # AstrBotConfig，依照 _conf_schema.json 生成，
        # 可在 AstrBot 管理面板 -> 插件 -> 本插件的“插件配置”中直接修改，
        # 无需再手动编辑任何文件
        self.config = config

        # gid -> unified_msg_origin，记录每个下载任务是由哪个会话发起的，
        # 用于任务完成/出错后主动通知
        self._tracked_gids = {}
        self._poll_task = None

    # ------------------------------------------------------------------
    # 配置读取（均来自插件配置面板，实时读取，无需重载插件）
    # ------------------------------------------------------------------

    @property
    def _aria2_url(self):
        return self.config.get("aria2_url", "http://127.0.0.1:6800/jsonrpc")

    @property
    def _aria2_token(self):
        return self.config.get("token", "")

    @property
    def _download_dir(self):
        return (self.config.get("download_dir", "") or "").strip()

    @property
    def _proxy(self):
        return (self.config.get("proxy", "") or "").strip()

    @property
    def _requests_proxies(self):
        proxy = self._proxy
        if not proxy:
            return None
        return {"http": proxy, "https": proxy}

    @property
    def _qq_notify(self):
        return self.config.get("qq_notify", True)

    @property
    def _notify_interval(self):
        return self.config.get("notify_interval", 10) or 10

    @property
    def _allowed_users(self):
        raw = self.config.get("allowed_users", []) or []
        return [str(u).strip() for u in raw if str(u).strip()]

    def _is_allowed(self, event: AstrMessageEvent) -> bool:
        """判断该会话发送者是否被允许使用下载功能。
        白名单为空时表示不限制，所有人都可以使用。
        """
        allowed = self._allowed_users
        if not allowed:
            return True
        return str(event.get_sender_id()) in allowed

    # ------------------------------------------------------------------
    # aria2 RPC 调用
    # ------------------------------------------------------------------

    def _aria2_call(self, method, params=None):
        if params is None:
            params = []
        else:
            params = list(params)

        if self._aria2_token:
            params.insert(0, "token:" + self._aria2_token)

        payload = {
            "jsonrpc": "2.0",
            "id": "astrbot",
            "method": method,
            "params": params
        }

        try:
            r = requests.post(
                self._aria2_url,
                json=payload,
                timeout=15,
                proxies=self._requests_proxies
            )
        except requests.exceptions.ProxyError as e:
            raise RuntimeError(f"代理连接失败，请检查代理地址配置: {e}")
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"请求 aria2 失败: {e}")

        r.raise_for_status()

        data = r.json()

        if "error" in data:
            err = data.get("error") or {}
            raise RuntimeError(err.get("message", "aria2 返回未知错误"))

        if "result" not in data:
            raise RuntimeError("aria2 未返回有效结果")

        return data

    def _add_uri(self, url, custom_dir=None):
        """添加下载任务，支持自定义保存目录。
        custom_dir 优先于插件配置中的默认下载目录；两者都为空则使用 aria2 自身默认目录。
        """
        target_dir = (custom_dir or "").strip() or self._download_dir

        params = [[url]]
        if target_dir:
            params.append({"dir": target_dir})

        return self._aria2_call("aria2.addUri", params)["result"]

    def _track(self, gid, event: AstrMessageEvent):
        """记录一个下载任务，以便后续主动通知其发起会话"""
        if self._qq_notify and gid:
            self._tracked_gids[gid] = event.unified_msg_origin

    # ------------------------------------------------------------------
    # 后台轮询 / 主动通知
    # ------------------------------------------------------------------

    @filter.on_astrbot_loaded()
    async def _start_notify_task(self):
        if self._poll_task is None:
            self._poll_task = asyncio.create_task(self._poll_downloads())

    async def terminate(self):
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None

    async def _poll_downloads(self):
        """后台轮询已追踪的下载任务，完成/出错/被移除时主动推送通知"""
        while True:
            try:
                await asyncio.sleep(self._notify_interval)

                if not self._qq_notify or not self._tracked_gids:
                    continue

                for gid in list(self._tracked_gids.keys()):
                    umo = self._tracked_gids.get(gid)

                    try:
                        info = self._aria2_call(
                            "aria2.tellStatus",
                            [gid, ["status", "totalLength", "files", "errorMessage"]]
                        )["result"]
                    except Exception:
                        # aria2 暂时不可达，稍后重试，不移除追踪
                        continue

                    status = info.get("status")

                    if status not in ("complete", "error", "removed"):
                        continue

                    filename = ""
                    files = info.get("files") or []
                    if files:
                        filename = os.path.basename(files[0].get("path", ""))

                    if status == "complete":
                        text = (
                            "✅ 下载已完成\n"
                            f"GID: {gid}\n"
                            f"文件: {filename or '未知'}"
                        )
                    elif status == "error":
                        text = (
                            "❌ 下载失败\n"
                            f"GID: {gid}\n"
                            f"原因: {info.get('errorMessage', '未知错误')}"
                        )
                    else:
                        text = f"🗑 下载任务已被移除\nGID: {gid}"

                    self._tracked_gids.pop(gid, None)

                    if not umo:
                        continue

                    try:
                        await self.context.send_message(
                            umo,
                            MessageChain().message(text)
                        )
                    except Exception:
                        pass

            except asyncio.CancelledError:
                break
            except Exception:
                # 轮询循环本身不应因单次异常而终止
                continue

    # ------------------------------------------------------------------
    # 指令
    # ------------------------------------------------------------------

    @filter.command("aria2测试")
    async def test(self, event: AstrMessageEvent):
        try:
            result = self._aria2_call("aria2.getVersion")
            yield event.plain_result(
                "✅ aria2连接成功\n"
                "版本: " +
                result["result"]["version"]
            )
        except Exception as e:
            yield event.plain_result(
                f"❌ aria2连接失败\n{e}"
            )


    @filter.command("下载")
    async def download(self, event: AstrMessageEvent):
        if not self._is_allowed(event):
            yield event.plain_result("⛔ 你没有权限使用下载功能")
            return

        raw = event.message_str.replace("/下载", "", 1).strip()

        if not raw:
            yield event.plain_result(
                "请输入下载链接，例如:\n"
                "/下载 <链接>\n"
                "/下载 <链接> <自定义保存目录>"
            )
            return

        parts = raw.split(maxsplit=1)
        url = parts[0]
        custom_dir = parts[1].strip() if len(parts) > 1 else None

        try:
            gid = self._add_uri(url, custom_dir)

            self._track(gid, event)

            text = "✅ 下载任务已添加\n" + f"GID:\n{gid}"

            effective_dir = custom_dir or self._download_dir
            if effective_dir:
                text += f"\n保存目录: {effective_dir}"

            yield event.plain_result(text)

        except Exception as e:
            yield event.plain_result(
                f"❌ 添加失败\n{e}"
            )


    @filter.command("下载列表")
    async def list_download(self, event: AstrMessageEvent):
        if not self._is_allowed(event):
            yield event.plain_result("⛔ 你没有权限使用下载功能")
            return

        try:
            active = self._aria2_call(
                "aria2.tellActive"
            )["result"]

            text = "📥 当前下载任务\n\n"

            if not active:
                text += "暂无任务"

            for item in active:
                text += (
                    f"状态: {item.get('status')}\n"
                    f"GID: {item.get('gid')}\n"
                    f"速度: {item.get('downloadSpeed')} B/s\n\n"
                )

            yield event.plain_result(text)

        except Exception as e:
            yield event.plain_result(
                f"查询失败\n{e}"
            )


    @filter.command("下载状态")
    async def status(self, event: AstrMessageEvent):
        if not self._is_allowed(event):
            yield event.plain_result("⛔ 你没有权限使用下载功能")
            return

        gid = event.message_str.replace("/下载状态", "", 1).strip()

        try:
            info = self._aria2_call(
                "aria2.tellStatus",
                [gid]
            )["result"]

            total = int(info.get("totalLength", 0))
            done = int(info.get("completedLength", 0))

            progress = (
                done / total * 100
                if total else 0
            )

            yield event.plain_result(
                f"📊 下载状态\n"
                f"状态: {info.get('status')}\n"
                f"进度: {progress:.2f}%\n"
                f"速度: {info.get('downloadSpeed')} B/s"
            )

        except Exception as e:
            yield event.plain_result(
                f"失败\n{e}"
            )


    @filter.command("取消下载")
    async def cancel(self, event: AstrMessageEvent):
        if not self._is_allowed(event):
            yield event.plain_result("⛔ 你没有权限使用下载功能")
            return

        gid = event.message_str.replace("/取消下载", "", 1).strip()

        try:
            self._aria2_call(
                "aria2.remove",
                [gid]
            )

            self._tracked_gids.pop(gid, None)

            yield event.plain_result(
                "🗑 已取消"
            )

        except Exception as e:
            yield event.plain_result(
                f"失败\n{e}"
            )


    # ------------------------------------------------------------------
    # 以下为供 AI（LLM）在对话中自动调用的工具（Function Calling）
    # 用户无需输入具体指令，只需用自然语言表达下载意图，
    # 模型会自行判断并调用对应工具完成操作。
    # 与指令一样，同样受用户白名单（allowed_users）限制。
    # ------------------------------------------------------------------

    @filter.llm_tool(name="aria2_add_download")
    async def llm_add_download(self, event: AstrMessageEvent, url: str, dir: str = ""):
        '''当用户希望下载某个文件、种子或链接时，添加一个新的 aria2 下载任务。
        返回结果中会包含真实的 GID，之后查询/取消该任务时必须使用这个真实 GID，禁止编造。

        Args:
            url(string): 需要下载的文件的 URL 链接（支持 http/https/磁力链接等 aria2 支持的类型）
            dir(string): 可选，自定义的下载保存目录（aria2 所在服务器上的绝对路径）。不填则使用插件配置中的默认下载目录
        '''
        if not self._is_allowed(event):
            return "该用户没有权限使用下载功能，请告知用户联系管理员。"

        try:
            gid = self._add_uri(url, dir)
            self._track(gid, event)

            effective_dir = (dir or "").strip() or self._download_dir
            result = f"下载任务添加成功，真实GID为: {gid}"
            if effective_dir:
                result += f"，保存目录: {effective_dir}"
            return result
        except Exception as e:
            return f"添加下载失败: {e}"


    @filter.llm_tool(name="aria2_list_downloads")
    async def llm_list_downloads(self, event: AstrMessageEvent):
        '''查询当前 aria2 中所有正在进行的下载任务及其状态、速度、真实GID。
        当用户询问"有哪些下载任务""下载得怎么样了"等问题时调用。
        '''
        if not self._is_allowed(event):
            return "该用户没有权限使用下载功能，请告知用户联系管理员。"

        try:
            active = self._aria2_call("aria2.tellActive")["result"]

            if not active:
                return "当前没有正在进行的下载任务"

            lines = []
            for item in active:
                lines.append(
                    f"GID: {item.get('gid')} "
                    f"状态: {item.get('status')} "
                    f"速度: {item.get('downloadSpeed')} B/s"
                )
            return "当前下载任务:\n" + "\n".join(lines)
        except Exception as e:
            return f"查询失败: {e}"


    @filter.llm_tool(name="aria2_get_status")
    async def llm_get_status(self, event: AstrMessageEvent, gid: str):
        '''查询指定 GID 的下载任务的详细状态和进度百分比。
        gid 必须是之前 aria2_add_download 或 aria2_list_downloads 返回过的真实GID，不允许编造。

        Args:
            gid(string): 需要查询的下载任务的 GID 编号
        '''
        if not self._is_allowed(event):
            return "该用户没有权限使用下载功能，请告知用户联系管理员。"

        try:
            info = self._aria2_call(
                "aria2.tellStatus",
                [gid]
            )["result"]

            total = int(info.get("totalLength", 0))
            done = int(info.get("completedLength", 0))
            progress = done / total * 100 if total else 0

            return (
                f"GID: {gid}\n"
                f"状态: {info.get('status')}\n"
                f"进度: {progress:.2f}%\n"
                f"速度: {info.get('downloadSpeed')} B/s"
            )
        except Exception as e:
            return f"查询失败: {e}（请确认该 GID 是否真实存在，不要使用编造的 GID）"


    @filter.llm_tool(name="aria2_cancel_download")
    async def llm_cancel_download(self, event: AstrMessageEvent, gid: str):
        '''取消（删除）指定 GID 的下载任务。
        当用户明确要求取消、停止或删除某个下载任务时调用。
        gid 必须是之前 aria2_add_download 或 aria2_list_downloads 返回过的真实GID，不允许编造。

        Args:
            gid(string): 需要取消的下载任务的 GID 编号
        '''
        if not self._is_allowed(event):
            return "该用户没有权限使用下载功能，请告知用户联系管理员。"

        try:
            self._aria2_call(
                "aria2.remove",
                [gid]
            )

            self._tracked_gids.pop(gid, None)

            return f"GID: {gid} 已成功取消"
        except Exception as e:
            return f"取消失败: {e}（请确认该 GID 是否真实存在，不要使用编造的 GID）"
