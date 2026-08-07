# boss_ocr.py
# 功能：ADB截图 → 裁剪上半部分 → RapidOCR识别文字

import os
import subprocess
import cv2
from rapidocr_onnxruntime import RapidOCR

# ADB路径
ADB_PATH = r"C:\Users\Administrator\Desktop\2.0\platform-tools\adb.exe"
# 截图保存路径
SCREENSHOT_PATH = r"C:\Users\Administrator\Desktop\2.0\screenshot.png"
CROP_PATH = r"C:\Users\Administrator\Desktop\2.0\screenshot_top.png"


def adb_screenshot():
    """ADB截图并拉取到本地"""
    # 手机端截图
    subprocess.run([ADB_PATH, "shell", "screencap", "-p", "/sdcard/screenshot.png"], check=True)
    # 拉取到本地
    subprocess.run([ADB_PATH, "pull", "/sdcard/screenshot.png", SCREENSHOT_PATH], check=True)
    # 删除手机端截图
    subprocess.run([ADB_PATH, "shell", "rm", "/sdcard/screenshot.png"])


def crop_top_half(image_path, save_path):
    """裁剪图片上半部分"""
    img = cv2.imread(image_path)
    h, w = img.shape[:2]
    top_half = img[:h//2, :]
    cv2.imwrite(save_path, top_half)
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
    print("正在截图...")
    adb_screenshot()
    print("正在裁剪上半部分...")
    crop_top_half(SCREENSHOT_PATH, CROP_PATH)
    print("正在OCR识别...")
    ocr_recognize(CROP_PATH)
