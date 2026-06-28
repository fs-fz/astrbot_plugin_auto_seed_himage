import asyncio
import os
import random
import shutil
import string
from datetime import datetime

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Node, Nodes, Plain
from astrbot.api.star import Context, Star, register

DEFAULT_SOURCE_DIR = "/AstrBot/files/source"
DEFAULT_TARGET_DIR = "/AstrBot/files/tmp"
DEFAULT_MAX_IMAGES = 10
MIN_SIZE = 512 * 1024
HASH_APPEND_LEN = 16
RENAME_LEN = 32
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
MERGE_FORWARD_PLATFORMS = {
    "aiocqhttp",
    "default",
    "onebot",
    "qq",
    "qq_official",
}
DAILY_STATE_KEY = "daily_push_last_slot"
SCHEDULER_INTERVAL_SECONDS = 20
EMPTY_IMAGE_NOTIFY_TARGET = "default:FriendMessage:393691734"


class NoImagesError(RuntimeError):
    pass


def rand_str(n: int) -> str:
    chars = string.ascii_letters + string.digits
    return "".join(random.choice(chars) for _ in range(n))


def pick_random_image(dir_path: str) -> str:
    if not os.path.isdir(dir_path):
        raise NoImagesError(f"来源目录不存在: {dir_path}")
    imgs = [
        f for f in os.listdir(dir_path)
        if f.lower().endswith(IMAGE_EXTS)
        and os.path.isfile(os.path.join(dir_path, f))
    ]
    if not imgs:
        raise NoImagesError(f"来源目录中没有可用图片: {dir_path}")
    return os.path.join(dir_path, random.choice(imgs))


def append_random_bytes(path: str, length: int) -> None:
    with open(path, "ab") as f:
        f.write(rand_str(length).encode())


def make_self_image_seed(path: str) -> None:
    # 自己 + 自己（图种）
    with open(path, "rb") as f:
        data = f.read()
    with open(path, "ab") as f:
        f.write(data)


def process_one_image(
    source_dir: str,
    target_dir: str,
    min_size: int,
) -> str:
    os.makedirs(target_dir, exist_ok=True)

    img_path = pick_random_image(source_dir)
    append_random_bytes(img_path, HASH_APPEND_LEN)

    while os.path.getsize(img_path) < min_size:
        make_self_image_seed(img_path)

    ext = os.path.splitext(img_path)[1]
    new_name = rand_str(RENAME_LEN) + ext
    target_path = os.path.abspath(os.path.join(target_dir, new_name))

    shutil.move(img_path, target_path)
    return target_path


def is_user_allowed(access_mode: str, user_ids: list, sender_id: str) -> bool:
    configured_ids = {
        str(user_id).strip()
        for user_id in user_ids
        if str(user_id).strip()
    }
    sender_id = str(sender_id).strip()

    if access_mode == "blacklist":
        return sender_id not in configured_ids
    if access_mode == "whitelist":
        return sender_id in configured_ids
    return True


def get_positive_int(config: AstrBotConfig, key: str, default: int) -> int:
    try:
        return max(1, int(config.get(key, default)))
    except (TypeError, ValueError):
        return default


def get_non_negative_int(config: dict, key: str, default: int) -> int:
    try:
        return max(0, int(config.get(key, default)))
    except (TypeError, ValueError):
        return default


def make_image_component(file_path: str) -> Image:
    if hasattr(Image, "fromFileSystem"):
        return Image.fromFileSystem(file_path)
    if hasattr(Image, "fromFile"):
        return Image.fromFile(file_path)
    return Image(file=file_path)


def make_forward_result(event: AstrMessageEvent, image_paths: list[str]):
    node_name = event.get_sender_name() or "随机图片"
    node_uin = str(event.get_sender_id() or "")
    nodes = [
        Node(
            content=[make_image_component(image_path)],
            name=node_name,
            uin=node_uin,
        )
        for image_path in image_paths
    ]
    return event.chain_result([Nodes(nodes=nodes)])


def make_message_chain(components: list) -> MessageChain:
    try:
        return MessageChain(chain=components)
    except TypeError:
        message_chain = MessageChain()
        message_chain.chain = components
        return message_chain


def parse_daily_time(value: object) -> tuple[int, int] | None:
    parts = str(value).strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = (int(part) for part in parts)
    except ValueError:
        return None
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return None


def parse_daily_times(value: object) -> tuple[list[tuple[int, int]], list[str]]:
    values = value if isinstance(value, list) else [value]
    valid_times = []
    invalid_values = []
    for item in values:
        parsed_time = parse_daily_time(item)
        if parsed_time is None:
            invalid_values.append(str(item))
        elif parsed_time not in valid_times:
            valid_times.append(parsed_time)
    return sorted(valid_times), invalid_values


def normalize_group_ids(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(
        dict.fromkeys(
            str(group_id).strip()
            for group_id in value
            if str(group_id).strip()
        )
    )


def build_group_targets(value: object) -> list[str]:
    targets = []
    for group_id in normalize_group_ids(value):
        if group_id.startswith("default:GroupMessage:"):
            targets.append(group_id)
        else:
            targets.append(f"default:GroupMessage:{group_id}")
    return targets


def target_supports_merge_forward(target: str) -> bool:
    platform_name = target.split(":", 1)[0].lower()
    return platform_name in MERGE_FORWARD_PLATFORMS


@register(
    "astrbot_plugin_auto_seed_himage",
    "fsfz",
    "从本地获取随机图片。使用 /img 数量 获取图片。",
    "1.7.3",
)
class SetuPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._scheduler_task = None
        self._last_daily_run_slot = ""
        self._last_invalid_daily_times = None
        self._last_daily_source_empty = False

    async def initialize(self):
        """启动每日主动发送调度器。"""
        if (
            self._scheduler_task is not None
            and not self._scheduler_task.done()
        ):
            return
        try:
            self._last_daily_run_slot = await self.get_kv_data(
                DAILY_STATE_KEY,
                "",
            )
        except Exception as e:
            logger.warning(f"读取每日发送状态失败，将仅在内存中去重: {e}")
        self._scheduler_task = asyncio.create_task(self._daily_scheduler())

    async def terminate(self):
        """停止每日主动发送调度器。"""
        if self._scheduler_task is None:
            return
        self._scheduler_task.cancel()
        try:
            await self._scheduler_task
        except asyncio.CancelledError:
            pass
        self._scheduler_task = None

    def _get_daily_config(self) -> dict:
        daily_config = self.config.get("daily_push", {})
        return daily_config if isinstance(daily_config, dict) else {}

    async def _daily_scheduler(self):
        while True:
            try:
                daily_config = self._get_daily_config()
                if daily_config.get("enabled", False):
                    configured_times = daily_config.get(
                        "times",
                        ["08:00"],
                    )
                    scheduled_times, invalid_values = parse_daily_times(
                        configured_times
                    )
                    invalid_key = tuple(invalid_values)
                    if invalid_key != self._last_invalid_daily_times:
                        if invalid_values:
                            logger.warning(
                                "以下每日发送时间格式无效，应为 HH:MM: "
                                + ", ".join(invalid_values)
                            )
                        self._last_invalid_daily_times = invalid_key

                    now = datetime.now().astimezone()
                    current_time = (now.hour, now.minute)
                    current_slot = now.strftime("%Y-%m-%d %H:%M")
                    if (
                        current_time in scheduled_times
                        and self._last_daily_run_slot != current_slot
                    ):
                        await self._run_daily_push(daily_config)
                        self._last_daily_run_slot = current_slot
                        try:
                            await self.put_kv_data(
                                DAILY_STATE_KEY,
                                current_slot,
                            )
                        except Exception as e:
                            logger.warning(f"保存每日发送状态失败: {e}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"每日图片调度器异常: {e}", exc_info=True)

            await asyncio.sleep(SCHEDULER_INTERVAL_SECONDS)

    async def _run_daily_push(self, daily_config: dict) -> bool:
        self._last_daily_source_empty = False
        group_ids = daily_config.get("group_ids", [])
        targets = build_group_targets(group_ids)
        if not targets:
            logger.warning("每日图片发送已启用，但未配置目标群号。")
            return False

        max_images = get_positive_int(
            self.config,
            "max_images",
            DEFAULT_MAX_IMAGES,
        )
        image_count = get_positive_int(daily_config, "count", 1)
        image_count = min(image_count, max_images)
        source_dir = str(
            self.config.get("source_dir", DEFAULT_SOURCE_DIR)
            or DEFAULT_SOURCE_DIR
        ).strip()
        target_dir = str(
            self.config.get("target_dir", DEFAULT_TARGET_DIR)
            or DEFAULT_TARGET_DIR
        ).strip()

        merge_forward = bool(daily_config.get("merge_forward", False))
        forward_name = str(
            daily_config.get("forward_name", "每日图片")
            or "每日图片"
        ).strip()
        send_interval = get_non_negative_int(
            daily_config,
            "send_interval",
            3,
        )
        retry_count = get_non_negative_int(
            daily_config,
            "retry_count",
            1,
        )
        retry_delay = get_non_negative_int(
            daily_config,
            "retry_delay",
            5,
        )

        sent_any = False
        processed_count = 0
        successful_targets = 0
        for target_index, target in enumerate(targets):
            image_paths = []
            source_empty = False
            try:
                for _ in range(image_count):
                    image_paths.append(
                        process_one_image(
                            source_dir,
                            target_dir,
                            MIN_SIZE,
                        )
                    )
            except NoImagesError as e:
                source_empty = True
                self._last_daily_source_empty = True
                logger.warning(str(e))
                if not image_paths:
                    await self._notify_no_images(
                        source_dir,
                        "每日图片发送",
                    )
                    break
                logger.warning(
                    f"目标 {target} 仅抽取到 {len(image_paths)} 张，"
                    "发送已有图片后终止本次每日任务。"
                )
            except Exception as e:
                if not image_paths:
                    logger.error(
                        f"每日图片处理失败，跳过目标 {target}: {e}",
                        exc_info=True,
                    )
                    continue
                logger.warning(
                    f"目标 {target} 仅成功处理 {len(image_paths)} 张，"
                    f"将发送已有图片: {e}"
                )
            processed_count += len(image_paths)

            try:
                if merge_forward and target_supports_merge_forward(target):
                    nodes = [
                        Node(
                            content=[make_image_component(image_path)],
                            name=forward_name,
                            uin=0,
                        )
                        for image_path in image_paths
                    ]
                    sent = await self._send_daily_message(
                        target,
                        make_message_chain([Nodes(nodes=nodes)]),
                        retry_count,
                        retry_delay,
                    )
                else:
                    sent = True
                    for image_index, image_path in enumerate(image_paths):
                        current_sent = await self._send_daily_message(
                            target,
                            make_message_chain(
                                [make_image_component(image_path)]
                            ),
                            retry_count,
                            retry_delay,
                        )
                        sent = sent and current_sent
                        if (
                            send_interval
                            and image_index < len(image_paths) - 1
                        ):
                            await asyncio.sleep(send_interval)

                if not sent:
                    logger.error(f"每日图片未能发送到目标: {target}")
                else:
                    sent_any = True
                    successful_targets += 1
            except Exception as e:
                logger.error(
                    f"向每日图片目标 {target} 发送失败: {e}",
                    exc_info=True,
                )
            if source_empty:
                await self._notify_no_images(
                    source_dir,
                    "每日图片发送",
                )
                break
            if send_interval and target_index < len(targets) - 1:
                await asyncio.sleep(send_interval)
        if sent_any:
            logger.info(
                f"每日图片发送完成: 共处理 {processed_count} 张, "
                f"{successful_targets}/{len(targets)} 个目标成功"
            )
        return sent_any

    async def _notify_no_images(
        self,
        source_dir: str,
        trigger: str,
    ) -> None:
        message = (
            f"图片来源目录已无可用图片，{trigger}已终止。\n"
            f"来源目录：{source_dir}"
        )
        try:
            sent = await self.context.send_message(
                EMPTY_IMAGE_NOTIFY_TARGET,
                make_message_chain([Plain(message)]),
            )
            if not sent:
                logger.error("未找到管理员私聊会话，无法发送无图片提醒。")
        except Exception as e:
            logger.error(f"发送无图片私聊提醒失败: {e}")

    async def _send_daily_message(
        self,
        target: str,
        message_chain: MessageChain,
        retry_count: int,
        retry_delay: int,
    ) -> bool:
        attempts = retry_count + 1
        for attempt in range(1, attempts + 1):
            try:
                if await self.context.send_message(target, message_chain):
                    return True
                error = RuntimeError("未找到匹配的平台会话")
            except Exception as e:
                error = e

            if attempt < attempts:
                logger.warning(
                    f"每日图片发送失败，将重试 {target} "
                    f"({attempt}/{attempts}): {error}"
                )
                if retry_delay:
                    await asyncio.sleep(retry_delay)
            else:
                logger.error(
                    f"每日图片发送最终失败 {target}: {error}"
                )
        return False

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("himg_daily_test")
    async def test_daily_push(self, event: AstrMessageEvent):
        """立即测试每日主动发送配置。"""
        daily_config = self._get_daily_config()
        group_ids = daily_config.get("group_ids", [])
        if not normalize_group_ids(group_ids):
            yield event.plain_result("尚未配置每日发送目标群号。")
            return
        sent = await self._run_daily_push(daily_config)
        if self._last_daily_source_empty:
            return
        if sent:
            yield event.plain_result("每日图片测试发送完成。")
        else:
            yield event.plain_result("每日图片测试发送失败，请检查日志。")

    @filter.command("img", alias={"himg"})
    async def get_setu(self, event: AstrMessageEvent, num: int):
        """从本地目录随机获取指定数量的图片。"""
        access_mode = str(self.config.get("access_mode", "disabled"))
        user_ids = self.config.get("user_ids", [])
        if not isinstance(user_ids, list):
            user_ids = []

        if not is_user_allowed(access_mode, user_ids, event.get_sender_id()):
            yield event.plain_result("你无权使用此命令。")
            return

        max_images = get_positive_int(
            self.config,
            "max_images",
            DEFAULT_MAX_IMAGES,
        )
        if num <= 0:
            yield event.plain_result("图片数量必须大于 0。")
            return
        if num > max_images:
            yield event.plain_result(f"单次最多获取 {max_images} 张图片。")
            return

        source_dir = str(
            self.config.get("source_dir", DEFAULT_SOURCE_DIR)
            or DEFAULT_SOURCE_DIR
        ).strip()
        target_dir = str(
            self.config.get("target_dir", DEFAULT_TARGET_DIR)
            or DEFAULT_TARGET_DIR
        ).strip()

        merge_forward = bool(self.config.get("merge_forward", False))
        platform_name = str(event.get_platform_name()).lower()
        use_merge_forward = (
            merge_forward
            and platform_name in MERGE_FORWARD_PLATFORMS
        )

        if not use_merge_forward:
            try:
                for _ in range(num):
                    image_path = process_one_image(
                        source_dir,
                        target_dir,
                        MIN_SIZE,
                    )
                    yield event.image_result(image_path)
            except NoImagesError:
                await self._notify_no_images(source_dir, "/himg")
                return
            except Exception as e:
                yield event.plain_result(f"请求失败: {e}")
            return

        image_paths = []
        try:
            for _ in range(num):
                image_path = process_one_image(
                    source_dir,
                    target_dir,
                    MIN_SIZE,
                )
                image_paths.append(image_path)
        except NoImagesError:
            await self._notify_no_images(source_dir, "/himg")
            if image_paths:
                yield make_forward_result(event, image_paths)
            return
        except Exception as e:
            if image_paths:
                yield make_forward_result(event, image_paths)
            yield event.plain_result(f"请求失败: {e}")
            return

        yield make_forward_result(event, image_paths)
