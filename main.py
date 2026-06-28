import asyncio
import os
import random
import shutil
import string
from datetime import datetime

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Node, Nodes
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
DAILY_STATE_KEY = "daily_push_last_date"
SCHEDULER_INTERVAL_SECONDS = 20


def rand_str(n: int) -> str:
    chars = string.ascii_letters + string.digits
    return "".join(random.choice(chars) for _ in range(n))


def pick_random_image(dir_path: str) -> str:
    imgs = [
        f for f in os.listdir(dir_path)
        if f.lower().endswith(IMAGE_EXTS)
        and os.path.isfile(os.path.join(dir_path, f))
    ]
    if not imgs:
        raise RuntimeError("目录中没有可用图片")
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
    "1.6.1",
)
class SetuPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._scheduler_task = None
        self._last_daily_run_date = ""
        self._last_invalid_daily_time = None

    async def initialize(self):
        """启动每日主动发送调度器。"""
        if (
            self._scheduler_task is not None
            and not self._scheduler_task.done()
        ):
            return
        try:
            self._last_daily_run_date = await self.get_kv_data(
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
                    scheduled_time = parse_daily_time(
                        daily_config.get("time", "08:00")
                    )
                    if scheduled_time is None:
                        invalid_value = str(daily_config.get("time", ""))
                        if invalid_value != self._last_invalid_daily_time:
                            logger.warning(
                                "每日发送时间格式无效，应为 HH:MM: "
                                f"{invalid_value}"
                            )
                            self._last_invalid_daily_time = invalid_value
                    else:
                        self._last_invalid_daily_time = None
                        now = datetime.now().astimezone()
                        today = now.date().isoformat()
                        if (
                            (now.hour, now.minute) == scheduled_time
                            and self._last_daily_run_date != today
                        ):
                            if await self._run_daily_push(daily_config):
                                self._last_daily_run_date = today
                                try:
                                    await self.put_kv_data(
                                        DAILY_STATE_KEY,
                                        today,
                                    )
                                except Exception as e:
                                    logger.warning(
                                        f"保存每日发送状态失败: {e}"
                                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"每日图片调度器异常: {e}", exc_info=True)

            await asyncio.sleep(SCHEDULER_INTERVAL_SECONDS)

    async def _run_daily_push(self, daily_config: dict) -> bool:
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

        image_paths = []
        try:
            for _ in range(image_count):
                image_paths.append(
                    process_one_image(source_dir, target_dir, MIN_SIZE)
                )
        except Exception as e:
            if not image_paths:
                logger.error(f"每日图片处理失败: {e}", exc_info=True)
                return False
            logger.warning(
                f"每日图片仅成功处理 {len(image_paths)} 张，将发送已有图片: {e}"
            )

        merge_forward = bool(daily_config.get("merge_forward", False))
        forward_name = str(
            daily_config.get("forward_name", "每日图片")
            or "每日图片"
        ).strip()

        sent_any = False
        for target in targets:
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
                    sent = await self.context.send_message(
                        target,
                        make_message_chain([Nodes(nodes=nodes)]),
                    )
                else:
                    sent = True
                    for image_path in image_paths:
                        current_sent = await self.context.send_message(
                            target,
                            make_message_chain(
                                [make_image_component(image_path)]
                            ),
                        )
                        sent = sent and current_sent

                if not sent:
                    logger.warning(f"未找到每日图片目标会话: {target}")
                else:
                    sent_any = True
            except Exception as e:
                logger.error(
                    f"向每日图片目标 {target} 发送失败: {e}",
                    exc_info=True,
                )
        if sent_any:
            logger.info(
                f"每日图片发送完成: {len(image_paths)} 张, "
                f"{len(targets)} 个目标"
            )
        return sent_any

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("himg_daily_test")
    async def test_daily_push(self, event: AstrMessageEvent):
        """立即测试每日主动发送配置。"""
        daily_config = self._get_daily_config()
        group_ids = daily_config.get("group_ids", [])
        if not normalize_group_ids(group_ids):
            yield event.plain_result("尚未配置每日发送目标群号。")
            return
        if await self._run_daily_push(daily_config):
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
        except Exception as e:
            if image_paths:
                yield make_forward_result(event, image_paths)
            yield event.plain_result(f"请求失败: {e}")
            return

        yield make_forward_result(event, image_paths)
