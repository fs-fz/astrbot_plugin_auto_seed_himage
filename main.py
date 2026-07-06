import asyncio
import os
import random
import shutil
import string
from datetime import datetime

try:
    from PIL import Image as PILImage
    from PIL import ImageOps
except ImportError:
    PILImage = None
    ImageOps = None

try:
    import img2pdf
except ImportError:
    img2pdf = None

try:
    import pikepdf
except ImportError:
    pikepdf = None

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Node, Nodes, Plain

try:
    from astrbot.api.message_components import File as FileComponent
except ImportError:
    FileComponent = None
from astrbot.api.star import Context, Star, register

DEFAULT_SOURCE_DIR = "/AstrBot/files/source"
DEFAULT_TARGET_DIR = "/AstrBot/files/tmp"
DEFAULT_PDF_DIR = "/home/fsfz/files/napcat"
DEFAULT_MAX_IMAGES = 10
MIN_SIZE = 512 * 1024
HASH_APPEND_LEN = 16
RENAME_LEN = 32
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
STITCH_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
MAX_STITCHED_SIZE = 20 * 1024 * 1024
STITCH_GAP = 8
STITCH_BACKGROUND = (255, 255, 255)
MERGE_FORWARD_PLATFORMS = {
    "aiocqhttp",
    "default",
    "onebot",
    "qq",
    "qq_official",
}
DAILY_STATE_KEY = "daily_push_last_slot"
SCHEDULER_INTERVAL_SECONDS = 20
PDF_PASSWORD_LENGTH = 4
EMPTY_IMAGE_NOTIFY_TARGET = "default:FriendMessage:393691734"
PDF_CLEANUP_DELAY_SECONDS = 300


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


def is_stitchable_image(image_path: str) -> bool:
    ext = os.path.splitext(image_path)[1].lower()
    if ext not in STITCH_IMAGE_EXTS:
        return False
    try:
        with PILImage.open(image_path) as image:
            if getattr(image, "is_animated", False):
                return False
            if getattr(image, "n_frames", 1) > 1:
                return False
    except Exception as e:
        logger.warning(f"检查图片是否可拼接失败，将保留原图发送 {image_path}: {e}")
        return False
    return True


def split_stitchable_images(image_paths: list[str]) -> tuple[list[str], list[str]]:
    stitchable_paths = []
    passthrough_paths = []
    for image_path in image_paths:
        if is_stitchable_image(image_path):
            stitchable_paths.append(image_path)
        else:
            passthrough_paths.append(image_path)
    return stitchable_paths, passthrough_paths


def _load_stitch_image(image_path: str):
    image = PILImage.open(image_path)
    image = ImageOps.exif_transpose(image)
    if image.mode in ("RGBA", "LA") or (
        image.mode == "P" and "transparency" in image.info
    ):
        background = PILImage.new("RGB", image.size, STITCH_BACKGROUND)
        background.paste(image.convert("RGBA"), mask=image.convert("RGBA").split()[-1])
        image.close()
        return background
    return image.convert("RGB")


def _save_compressed_jpeg(image, output_path: str) -> None:
    for quality in (92, 88, 84, 80, 76, 72, 68, 64, 60, 56, 52, 48, 44, 40):
        image.save(output_path, "JPEG", quality=quality, optimize=True)
        if os.path.getsize(output_path) <= MAX_STITCHED_SIZE:
            return

    current = image
    scale = 0.9
    while os.path.getsize(output_path) > MAX_STITCHED_SIZE and scale >= 0.45:
        resized = current.resize(
            (
                max(1, int(current.width * scale)),
                max(1, int(current.height * scale)),
            ),
            PILImage.LANCZOS,
        )
        if current is not image:
            current.close()
        current = resized
        current.save(output_path, "JPEG", quality=82, optimize=True)
        scale -= 0.08

    if current is not image:
        current.close()


def stitch_images(image_paths: list[str], target_dir: str) -> str | None:
    if len(image_paths) <= 1:
        return None
    if PILImage is None:
        logger.warning("Pillow 未安装，无法拼接图片，将按原方式发送。")
        return None

    images = []
    resized_images = []
    canvas = None
    try:
        for image_path in image_paths:
            images.append(_load_stitch_image(image_path))

        max_cell_width = min(1600, max(image.width for image in images))
        for image in images:
            if image.width > max_cell_width:
                height = max(1, int(image.height * max_cell_width / image.width))
                resized = image.resize((max_cell_width, height), PILImage.LANCZOS)
                image.close()
                resized_images.append(resized)
            else:
                resized_images.append(image)

        canvas_width = max(image.width for image in resized_images)
        canvas_height = (
            sum(image.height for image in resized_images)
            + STITCH_GAP * (len(resized_images) - 1)
        )
        canvas = PILImage.new(
            "RGB",
            (canvas_width, canvas_height),
            STITCH_BACKGROUND,
        )

        y = 0
        for image in resized_images:
            x = (canvas_width - image.width) // 2
            canvas.paste(image, (x, y))
            y += image.height + STITCH_GAP

        os.makedirs(target_dir, exist_ok=True)
        output_path = os.path.abspath(
            os.path.join(target_dir, rand_str(RENAME_LEN) + "_stitched.jpg")
        )
        canvas.save(output_path, "JPEG", quality=95, optimize=True)
        if os.path.getsize(output_path) > MAX_STITCHED_SIZE:
            _save_compressed_jpeg(canvas, output_path)
        return output_path
    except Exception as e:
        logger.error(f"拼接图片失败，将按原方式发送: {e}", exc_info=True)
        return None
    finally:
        close_targets = {id(image): image for image in images + resized_images}
        if canvas is not None:
            close_targets[id(canvas)] = canvas
        for image in close_targets.values():
            try:
                image.close()
            except Exception:
                pass


def build_send_image_paths(
    image_paths: list[str],
    target_dir: str,
    stitch_images_enabled: bool,
) -> list[str]:
    if not stitch_images_enabled or len(image_paths) <= 1:
        return image_paths
    if PILImage is None:
        logger.warning("Pillow 未安装，无法拼接图片，将按原方式发送。")
        return image_paths

    stitchable_paths, passthrough_paths = split_stitchable_images(image_paths)
    if len(stitchable_paths) <= 1:
        return image_paths

    stitched_image_path = stitch_images(stitchable_paths, target_dir)
    if not stitched_image_path:
        return image_paths
    return [stitched_image_path] + passthrough_paths


def generate_encrypted_pdf(
    image_paths: list[str],
    pdf_path: str,
    password: str,
) -> str | None:
    """将图片列表生成加密 PDF。

    先用 img2pdf 无损嵌入图片，再用 pypdf AES-256 加密。
    加密失败时回退到未加密 PDF。

    Returns:
        pdf_path 成功，None 失败。
    """
    if img2pdf is None:
        logger.warning("img2pdf 未安装，无法生成 PDF。请 pip install img2pdf。")
        return None
    if not image_paths:
        logger.warning("没有图片，跳过 PDF 生成。")
        return None

    valid_paths = []
    for path in image_paths:
        if os.path.isfile(path) and path.lower().endswith(IMAGE_EXTS):
            valid_paths.append(path)
        else:
            logger.warning(f"跳过无法用于 PDF 的文件: {path}")
    if not valid_paths:
        logger.warning("没有可用的图片文件用于生成 PDF。")
        return None

    # 图片可能被 process_one_image 追加了随机字节和图种，img2pdf 无法解析。
    # 用 PIL 读取后保存干净副本再传入。
    os.makedirs(os.path.dirname(pdf_path), exist_ok=True)
    clean_paths = []
    clean_temp_files = []
    if PILImage is not None:
        for path in valid_paths:
            try:
                img = PILImage.open(path)
                img.load()  # 强制解码，忽略尾部垃圾
                clean_path = os.path.join(
                    os.path.dirname(pdf_path),
                    rand_str(16) + ".jpg",
                )
                img.convert("RGB").save(clean_path, "JPEG", quality=95)
                clean_paths.append(clean_path)
                clean_temp_files.append(clean_path)
            except Exception as e:
                logger.warning(f"无法清洗图片 {path}，跳过: {e}")
    else:
        clean_paths = valid_paths

    if not clean_paths:
        logger.warning("清洗后没有可用的图片用于生成 PDF。")
        return None

    unencrypted_path = pdf_path + ".tmp"
    try:
        pdf_bytes = img2pdf.convert(clean_paths)
        with open(unencrypted_path, "wb") as f:
            f.write(pdf_bytes)
    except Exception as e:
        logger.error(f"img2pdf 生成 PDF 失败: {e}", exc_info=True)
        return None
    finally:
        for p in clean_temp_files:
            cleanup_file(p)

    try:
        if pikepdf is not None:
            with pikepdf.open(unencrypted_path) as pdf:
                pdf.save(
                    pdf_path,
                    encryption=pikepdf.Encryption(
                        user=password,
                        owner=rand_str(16),
                    ),
                )
        else:
            logger.warning("pikepdf 未安装，回退为未加密 PDF。")
            os.replace(unencrypted_path, pdf_path)
    except Exception as e:
        logger.error(f"PDF 加密失败: {e}", exc_info=True)
        try:
            os.replace(unencrypted_path, pdf_path)
            logger.warning("加密失败，已回退为未加密 PDF。")
            return pdf_path
        except Exception as e2:
            logger.error(f"回退 PDF 写入失败: {e2}")
            return None
    finally:
        try:
            if os.path.isfile(unencrypted_path):
                os.remove(unencrypted_path)
        except Exception:
            pass

    return pdf_path


def generate_himg_password() -> str:
    """生成 /himg PDF 的随机短密码（如 '4829'）。"""
    return "".join(str(random.randint(0, 9)) for _ in range(PDF_PASSWORD_LENGTH))


def build_pdf_message_chain(
    pdf_path: str,
    pdf_filename: str,
    password_text: str,
):
    """组装「密码文本 + PDF 文件」的消息链。

    File 组件不可用时回退为纯文本（至少告知密码）。
    """
    if FileComponent is None:
        logger.warning("File 消息组件不可用，将仅发送密码文本。")
        return make_message_chain([Plain(password_text)])

    try:
        file_path = os.path.abspath(pdf_path)
        if not os.path.isfile(file_path):
            raise FileNotFoundError(file_path)
    except Exception as e:
        logger.error(f"PDF 文件不可用: {e}", exc_info=True)
        return make_message_chain([Plain(password_text)])

    return make_message_chain([
        Plain(password_text),
        FileComponent(name=pdf_filename, file=file_path),
    ])


def cleanup_file(file_path: str) -> None:
    """安全清理临时文件，不抛异常。"""
    try:
        if file_path and os.path.isfile(file_path):
            os.remove(file_path)
    except Exception as e:
        logger.warning(f"清理临时文件失败 {file_path}: {e}")


async def _cleanup_file_later(file_path: str, delay_seconds: int) -> None:
    await asyncio.sleep(delay_seconds)
    cleanup_file(file_path)


def schedule_cleanup_file(
    file_path: str,
    delay_seconds: int = PDF_CLEANUP_DELAY_SECONDS,
) -> None:
    try:
        asyncio.create_task(_cleanup_file_later(file_path, delay_seconds))
    except RuntimeError:
        cleanup_file(file_path)


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


def get_pdf_dir(config: AstrBotConfig, daily_config: dict | None = None) -> str:
    daily_config = daily_config if isinstance(daily_config, dict) else {}
    value = (
        daily_config.get("pdf_dir")
        or config.get("pdf_dir", DEFAULT_PDF_DIR)
        or DEFAULT_PDF_DIR
    )
    return str(value).strip() or DEFAULT_PDF_DIR


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
    "1.8.0",
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

        pdf_dir = get_pdf_dir(self.config, daily_config)

        merge_forward = bool(daily_config.get("merge_forward", False))
        stitch_images_enabled = bool(daily_config.get("stitch_images", False))
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

            pdf_enabled = bool(daily_config.get("pdf_enabled", False))
            send_image_paths = []
            try:
                if pdf_enabled:
                    # 只发 PDF：静态图进 PDF，动态图单发
                    send_image_paths = [
                        p for p in image_paths
                        if not is_stitchable_image(p)
                    ]
                else:
                    send_image_paths = build_send_image_paths(
                        image_paths,
                        target_dir,
                        stitch_images_enabled,
                    )
                if not send_image_paths:
                    # 全是静态图且已进 PDF，无需单独发图
                    sent = True

                elif len(send_image_paths) == 1:
                    sent = await self._send_daily_message(
                        target,
                        make_message_chain(
                            [make_image_component(send_image_paths[0])]
                        ),
                        retry_count,
                        retry_delay,
                    )
                elif merge_forward and target_supports_merge_forward(target):
                    nodes = []
                    pdf_path_cleanup = None
                    if pdf_enabled:
                        now = datetime.now().astimezone()
                        date_str = now.strftime("%y%m%d")
                        pdf_password = date_str
                        pdf_filename = (
                            f"每日图片_{date_str}_密码是日期.pdf"
                        )
                        static_paths = [
                            p for p in image_paths
                            if is_stitchable_image(p)
                        ]
                        if static_paths:
                            pdf_path = os.path.abspath(
                                os.path.join(
                                    pdf_dir,
                                    rand_str(RENAME_LEN) + "_daily.pdf",
                                )
                            )
                            if generate_encrypted_pdf(
                                static_paths, pdf_path, pdf_password
                            ):
                                pdf_node = self._build_pdf_node(
                                    pdf_path,
                                    pdf_filename,
                                    pdf_password,
                                    forward_name,
                                    0,
                                )
                                if pdf_node:
                                    nodes.append(pdf_node)
                                pdf_path_cleanup = pdf_path
                    nodes.extend(
                        Node(
                            content=[make_image_component(image_path)],
                            name=forward_name,
                            uin=0,
                        )
                        for image_path in send_image_paths
                    )
                    sent = await self._send_daily_message(
                        target,
                        make_message_chain([Nodes(nodes=nodes)]),
                        retry_count,
                        retry_delay,
                    )
                    if pdf_path_cleanup:
                        schedule_cleanup_file(pdf_path_cleanup)
                else:
                    sent = True
                    for image_index, image_path in enumerate(send_image_paths):
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
                            and image_index < len(send_image_paths) - 1
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

            # 合并转发时 PDF 已作为节点嵌入，这里不再单独发送
            pdf_in_forward = (
                pdf_enabled
                and merge_forward
                and target_supports_merge_forward(target)
            )
            if pdf_enabled and image_paths and not pdf_in_forward:
                try:
                    now = datetime.now().astimezone()
                    date_str = now.strftime("%y%m%d")
                    pdf_password = date_str
                    pdf_filename = f"每日图片_{date_str}_密码是日期.pdf"
                    pdf_sent = await self._send_daily_pdf(
                        target,
                        image_paths,
                        pdf_dir,
                        pdf_password,
                        pdf_filename,
                        retry_count,
                        retry_delay,
                    )
                    if pdf_sent:
                        logger.info(
                            f"每日图片 PDF 已发送到 {target}: {pdf_filename}"
                        )
                except Exception as e:
                    logger.error(
                        f"每日图片 PDF 发送失败 (目标: {target}): {e}",
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

    def _make_himg_pdf_info(
        self,
        image_paths: list[str],
        pdf_dir: str,
        sender_id: str,
    ):
        """生成 /himg 加密 PDF，返回 (pdf_path, pdf_filename, password)。

        失败返回 None。调用者负责清理 pdf_path。
        """
        if not bool(self.config.get("himg_pdf_enabled", False)):
            return None
        if not image_paths:
            return None

        static_paths = [p for p in image_paths if is_stitchable_image(p)]
        if not static_paths:
            return None

        num_images = len(static_paths)
        password = generate_himg_password()
        pdf_filename = f"{sender_id}_{num_images}_{password}.pdf"
        pdf_path = os.path.abspath(
            os.path.join(pdf_dir, rand_str(RENAME_LEN) + "_himg.pdf")
        )

        result = generate_encrypted_pdf(static_paths, pdf_path, password)
        if result is None:
            return None

        return pdf_path, pdf_filename, password, num_images, static_paths

    @staticmethod
    def _build_pdf_node(
        pdf_path: str,
        pdf_filename: str,
        password: str,
        node_name: str,
        node_uin,
    ) -> Node | None:
        """构建包含 PDF 文件和密码文本的聊天记录 Node。

        File 组件不可用时回退为仅含密码文本的 Node。
        """
        content = [Plain(f"PDF 密码: {password}\n文件名: {pdf_filename}")]
        if FileComponent is not None:
            try:
                file_path = os.path.abspath(pdf_path)
                if not os.path.isfile(file_path):
                    raise FileNotFoundError(file_path)
                content.append(
                    FileComponent(name=pdf_filename, file=file_path)
                )
            except Exception as e:
                logger.error(f"构建 PDF 节点失败: {e}", exc_info=True)
                # 至少保留密码文本
        return Node(
            content=content,
            name=node_name,
            uin=node_uin,
        )

    def _send_himg_pdf(
        self,
        event: AstrMessageEvent,
        image_paths: list[str],
        pdf_dir: str,
        sender_id: str,
    ):
        """生成器：非合并转发时发送独立 PDF 消息。"""
        pdf_info = self._make_himg_pdf_info(image_paths, pdf_dir, sender_id)
        if pdf_info is None:
            return

        pdf_path, pdf_filename, password, num_images, static_paths = pdf_info
        try:
            password_text = (
                f"PDF 已加密，共 {num_images} 张图片。\n"
                f"文件名: {pdf_filename}\n"
                f"密码: {password}"
            )
            message_chain = build_pdf_message_chain(
                pdf_path, pdf_filename, password_text
            )
            if message_chain is None:
                yield event.plain_result(
                    f"PDF 已生成 ({pdf_filename})，密码: {password}，"
                    "但当前平台不支持发送文件。"
                )
                return

            yield event.chain_result(message_chain.chain)
        except Exception as e:
            logger.error(f"/himg PDF 发送失败: {e}", exc_info=True)
            try:
                yield event.plain_result(f"加密 PDF 发送失败: {e}")
            except Exception:
                pass
        finally:
            schedule_cleanup_file(pdf_path)

    async def _send_daily_pdf(
        self,
        target: str,
        image_paths: list[str],
        pdf_dir: str,
        password: str,
        pdf_filename: str,
        retry_count: int,
        retry_delay: int,
    ) -> bool:
        """生成每日推送加密 PDF 并通过 _send_daily_message 发送。

        Returns:
            True 发送成功，False 失败。
        """
        if not image_paths:
            return False

        # 只把静态图加入 PDF，动态图依旧单发
        static_paths = [p for p in image_paths if is_stitchable_image(p)]
        if not static_paths:
            return False

        pdf_path = os.path.abspath(
            os.path.join(pdf_dir, rand_str(RENAME_LEN) + "_daily.pdf")
        )

        try:
            result = generate_encrypted_pdf(static_paths, pdf_path, password)
            if result is None:
                return False

            password_text = (
                f"每日图片 PDF 已加密\n"
                f"密码: {password}\n"
                f"文件名: {pdf_filename}"
            )
            message_chain = build_pdf_message_chain(
                pdf_path, pdf_filename, password_text
            )
            return await self._send_daily_message(
                target,
                message_chain,
                retry_count,
                retry_delay,
            )
        except Exception as e:
            logger.error(
                f"每日图片 PDF 生成或发送失败 (目标: {target}): {e}",
                exc_info=True,
            )
            return False
        finally:
            schedule_cleanup_file(pdf_path)

    def _make_forward_with_pdf(
        self,
        event: AstrMessageEvent,
        image_paths: list[str],
        target_dir: str,
        pdf_dir: str,
        stitch_images_enabled: bool,
    ):
        """生成器：构建含 PDF 节点的合并转发聊天记录并 yield。

        仅当 himg_pdf_enabled 开启且有静态图时才包含 PDF 节点。
        """
        pdf_info = self._make_himg_pdf_info(
            image_paths, pdf_dir, event.get_sender_id()
        )
        node_name = event.get_sender_name() or "随机图片"
        node_uin = str(event.get_sender_id() or "")
        nodes = []
        if pdf_info:
            pdf_path, pdf_filename, pdf_password, _, static_paths = pdf_info
            pdf_node = self._build_pdf_node(
                pdf_path, pdf_filename, pdf_password, node_name, node_uin
            )
            if pdf_node:
                nodes.append(pdf_node)
        if pdf_info:
            static_path_set = set(static_paths)
            send_image_paths = [
                path for path in image_paths
                if path not in static_path_set
            ]
        else:
            send_image_paths = build_send_image_paths(
                image_paths, target_dir, stitch_images_enabled
            )
        for image_path in send_image_paths:
            nodes.append(
                Node(
                    content=[make_image_component(image_path)],
                    name=node_name,
                    uin=node_uin,
                )
            )
        yield event.chain_result([Nodes(nodes=nodes)])
        if pdf_info:
            schedule_cleanup_file(pdf_info[0])

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

        pdf_dir = get_pdf_dir(self.config)

        merge_forward = bool(self.config.get("merge_forward", False))
        stitch_images_enabled = bool(self.config.get("stitch_images", False))
        platform_name = str(event.get_platform_name()).lower()
        use_merge_forward = (
            merge_forward
            and platform_name in MERGE_FORWARD_PLATFORMS
        )

        if not use_merge_forward:
            image_paths = []
            try:
                for _ in range(num):
                    image_paths.append(
                        process_one_image(
                            source_dir,
                            target_dir,
                            MIN_SIZE,
                        )
                    )
            except NoImagesError:
                await self._notify_no_images(source_dir, "/himg")
            except Exception as e:
                if not image_paths:
                    yield event.plain_result(f"请求失败: {e}")
                    return
                yield event.plain_result(f"请求失败: {e}")

            pdf_static_paths = []
            if image_paths:
                pdf_info = self._make_himg_pdf_info(
                    image_paths,
                    pdf_dir,
                    event.get_sender_id(),
                )
                if pdf_info:
                    pdf_path, pdf_filename, password, num_images, pdf_static_paths = pdf_info
                    try:
                        password_text = (
                            f"PDF 已加密，共 {num_images} 张图片。\n"
                            f"文件名: {pdf_filename}\n"
                            f"密码: {password}"
                        )
                        message_chain = build_pdf_message_chain(
                            pdf_path, pdf_filename, password_text
                        )
                        yield event.chain_result(message_chain.chain)
                    except Exception as e:
                        logger.error(f"/himg PDF 发送失败: {e}", exc_info=True)
                        try:
                            yield event.plain_result(f"加密 PDF 发送失败: {e}")
                        except Exception:
                            pass
                        pdf_static_paths = []
                    finally:
                        schedule_cleanup_file(pdf_path)

            if pdf_static_paths:
                pdf_static_path_set = set(pdf_static_paths)
                send_image_paths = [
                    path for path in image_paths
                    if path not in pdf_static_path_set
                ]
            else:
                send_image_paths = build_send_image_paths(
                    image_paths,
                    target_dir,
                    stitch_images_enabled,
                )
            for image_path in send_image_paths:
                yield event.image_result(image_path)
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
                for _result in self._make_forward_with_pdf(
                    event, image_paths, target_dir, pdf_dir, stitch_images_enabled
                ):
                    yield _result
            return
        except Exception as e:
            if image_paths:
                for _result in self._make_forward_with_pdf(
                    event, image_paths, target_dir, pdf_dir, stitch_images_enabled
                ):
                    yield _result
            yield event.plain_result(f"请求失败: {e}")
            return

        for _result in self._make_forward_with_pdf(
            event, image_paths, target_dir, pdf_dir, stitch_images_enabled
        ):
            yield _result
