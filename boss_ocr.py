# boss_ocr.py
# 功能：截图 → 裁剪上半部分 → RapidOCR识别文字
# 注意：截图和设备操作已迁移到 device_driver.py，由 boss_gui.py 统一调用
# 本文件仅保留裁剪和OCR功能，供独立测试使用

import os
import numpy as np
import cv2
from rapidocr_onnxruntime import RapidOCR

# 截图保存路径
SCREENSHOT_PATH = os.path.join(os.environ.get("TEMP", os.path.dirname(os.path.abspath(__file__))), "boss_screenshot.png")
CROP_PATH = os.path.join(os.environ.get("TEMP", os.path.dirname(os.path.abspath(__file__))), "screenshot_top.png")


def cv2_imread(path):
    """cv2.imread 不支持中文/空格路径，用 numpy.fromfile + cv2.imdecode 替代"""
    try:
        buf = np.fromfile(path, dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        return img
    except Exception:
        return None


def crop_top_half(image_path, save_path=None):
    """裁剪图片上半部分"""
    img = cv2_imread(image_path)
    if img is None:
        raise FileNotFoundError(f"截图文件读取失败: {image_path}")
    h, w = img.shape[:2]
    top_half = img[:h//2, :]
    if save_path:
        # 用 imencode + tofile 写入，兼容中文路径
        ext = os.path.splitext(save_path)[1] or '.png'
        result, buf = cv2.imencode(ext, top_half)
        if result:
            buf.tofile(save_path)
    return top_half


def ocr_recognize(image_path):
    """RapidOCR识别文字"""
    ocr = RapidOCR()
    result, _ = ocr(image_path)
    if result:
        for item in result:
            box, text, confidence = item
            print(f"文字: {text}  置信度: {confidence:.4f}  坐标: {box}")
    else:
        print("未识别到文字")


if __name__ == "__main__":
    print("注意：截图功能已迁移到 device_driver.py，请通过 boss_gui.py 使用")
    print("如需独立测试，请先手动截图保存到:", SCREENSHOT_PATH)
    if os.path.isfile(SCREENSHOT_PATH):
        print("正在裁剪上半部分...")
        crop_top_half(SCREENSHOT_PATH, CROP_PATH)
        print("正在OCR识别...")
        ocr_recognize(CROP_PATH)
    else:
        print("截图文件不存在，请先截图")
