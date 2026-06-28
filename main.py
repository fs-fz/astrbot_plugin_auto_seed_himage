import os
import random
import shutil
import string

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
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
    "onebot",
    "qq",
    "qq_official",
}


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


def process_one_image(source_dir: str, target_dir: str, min_size: int) -> str:
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


@register(
    "astrbot_plugin_auto_seed_himage",
    "fsfz",
    "从本地获取随机图片。使用 /img 数量 获取图片。",
    "1.5.0",
)
class SetuPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

    @filter.command("img")
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
                image_path = process_one_image(source_dir, target_dir, MIN_SIZE)
                image_paths.append(image_path)
        except Exception as e:
            if image_paths:
                yield make_forward_result(event, image_paths)
            yield event.plain_result(f"请求失败: {e}")
            return

        yield make_forward_result(event, image_paths)
