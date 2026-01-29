from astrbot.api.message_components import *
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
import aiohttp

import os
import random
import string
import shutil

# ================== 可配置变量 ==================
SOURCE_DIR = "/AstrBot/data/files/source"  # 随机选图的目录
TARGET_DIR = "/AstrBot/data/files/tmp"  # 移动后的目录
MIN_SIZE = 512 * 1024  # 最小大小（字节），如 100KB
HASH_APPEND_LEN = 16  # 改 hash 用的随机长度
RENAME_LEN = 32  # 重命名随机长度
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp",".gif")


# ===============================================


def rand_str(n: int) -> str:
    chars = string.ascii_letters + string.digits
    return ''.join(random.choice(chars) for _ in range(n))


def pick_random_image(dir_path: str) -> str:
    imgs = [
        f for f in os.listdir(dir_path)
        if f.lower().endswith(IMAGE_EXTS)
           and os.path.isfile(os.path.join(dir_path, f))
    ]
    if not imgs:
        raise RuntimeError("目录中没有可用图片")
    return os.path.join(dir_path, random.choice(imgs))


def append_random_bytes(path: str, length: int):
    with open(path, "ab") as f:
        f.write(rand_str(length).encode())


def make_self_image_seed(path: str):
    # 自己 + 自己（图种）
    with open(path, "rb") as f:
        data = f.read()
    with open(path, "ab") as f:
        f.write(data)


def process_one_image(
        source_dir: str,
        target_dir: str,
        min_size: int
) -> str:
    os.makedirs(target_dir, exist_ok=True)

    # 1. 随机选图
    img_path = pick_random_image(source_dir)

    # 2. 改 hash（所有图片都做）
    append_random_bytes(img_path, HASH_APPEND_LEN)

    # 3. 判断大小，过小就图种
    if os.path.getsize(img_path) < min_size:
        make_self_image_seed(img_path)

    # 4. 随机重命名
    ext = os.path.splitext(img_path)[1]
    new_name = rand_str(RENAME_LEN) + ext
    target_path = os.path.abspath(os.path.join(target_dir, new_name))

    # 5. 移动文件
    shutil.move(img_path, target_path)

    # 6. 返回最终路径
    return target_path


@register("astrbot_plugin_auto_seed_himage", "fsfz", "从本地获取随机图片。使用 /img 数量 获取一张随机图片。", "1.0")
class SetuPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @filter.command("img")
    async def get_setu(self, event: AstrMessageEvent, num: int):

        try:
            for i in range(num):
                #拿图
                image_url= process_one_image(SOURCE_DIR, TARGET_DIR, min_size=MIN_SIZE)
                # 发送图片
                yield event.image_result(image_url)

        except Exception as e:
            yield event.plain_result(f"\n请求失败: {str(e)}")
