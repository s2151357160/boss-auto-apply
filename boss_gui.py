# boss_gui.py
# 功能：BOSS直聘自动投递GUI - 关键词过滤 + 薪资颜色筛选 + 自动投递

import os
import sys

# 兼容PyInstaller/Nuitka打包：frozen模式下使用exe所在目录，否则使用脚本所在目录
if getattr(sys, 'frozen', False):
    WORK_DIR = os.path.dirname(sys.executable)
    # PyInstaller单文件模式下，datas解压到sys._MEIPASS临时目录
    # 需要将_MEIPASS/libs加入sys.path，使rapidocr_onnxruntime等包可被import
    _meipass = getattr(sys, '_MEIPASS', '')
    if _meipass:
        _libs_in_meipass = os.path.join(_meipass, 'libs')
        if os.path.isdir(_libs_in_meipass) and _libs_in_meipass not in sys.path:
            sys.path.insert(0, _libs_in_meipass)
else:
    # 自动添加libs目录到Python搜索路径，实现便携部署
    _libs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "libs")
    if os.path.isdir(_libs_dir) and _libs_dir not in sys.path:
        sys.path.insert(0, _libs_dir)
    WORK_DIR = os.path.dirname(os.path.abspath(__file__))

import re
import threading
import time
import random
import datetime
import json
import cv2
import numpy as np
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from rapidocr_onnxruntime import RapidOCR
from device_driver import create_driver, PLATFORM_OPTIONS

# ============ 配置 ============
# WORK_DIR 已在文件顶部根据frozen/脚本模式设置
SCREENSHOT_PATH = os.path.join(os.environ.get("TEMP", WORK_DIR), "boss_screenshot.png")
SUBPROC_FLAGS = 0x08000000  # CREATE_NO_WINDOW，隐藏命令行窗口

POPUP_KEYWORDS = ["今日投递上限", "投递上限", "请先完善简历", "完善简历", "操作频繁", "账号异常"]

GREEN_HSV_MIN = (80, 100, 130)
GREEN_HSV_MAX = (110, 255, 255)

_ocr_engine = None

def _get_ocr():
    global _ocr_engine
    if _ocr_engine is None:
        _ocr_engine = RapidOCR()
    return _ocr_engine


# ============ 设备操作（统一驱动接口） ============

_driver = None  # 全局设备驱动实例（由 BossGUI._apply_platform 创建）


def _set_driver(driver):
    """设置全局设备驱动实例"""
    global _driver
    _driver = driver


def _device_check():
    """检查设备连接，返回 (ok, msg)"""
    return _driver.check_connection()


def adb_screenshot(max_retry=3):
    """截图并保存到本地，失败自动重试"""
    ok, msg = _driver.screenshot(SCREENSHOT_PATH, max_retry=max_retry)
    if not ok:
        if "超时" in msg:
            raise TimeoutError(msg)
        else:
            raise ConnectionError(msg)


def adb_tap(x, y, offset=10):
    """随机偏移点击，坐标下限保护"""
    _driver.tap(x, y, offset)


def adb_swipe(x1, y1, x2, y2, duration=300):
    """滑动操作"""
    _driver.swipe(x1, y1, x2, y2, duration)


def adb_get_screen_size():
    """获取屏幕分辨率（缓存结果）"""
    return _driver.get_screen_size()


# ============ 兼容中文/空格路径的图像读取 ============
def cv2_imread(path):
    """cv2.imread 不支持中文/空格路径，用 numpy.fromfile + cv2.imdecode 替代"""
    try:
        buf = np.fromfile(path, dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        return img
    except Exception:
        return None


# ============ 裁剪上半部分 ============
def crop_top_half(image_path):
    """裁剪图片上半部分，返回numpy数组（不写盘）"""
    img = cv2_imread(image_path)
    if img is None:
        raise FileNotFoundError(f"截图文件读取失败: {image_path}")
    h, w = img.shape[:2]
    return img[:h // 2, :]


# ============ 墨绿色过滤 ============
def filter_green_color_img(img):
    """用HSV范围过滤墨绿色（内存版，输入输出都是numpy数组）"""
    return _apply_green_filter(img)


def _apply_green_filter(img):
    """核心HSV过滤逻辑，返回处理后的numpy数组"""
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    lower = np.array(GREEN_HSV_MIN)
    upper = np.array(GREEN_HSV_MAX)
    mask = cv2.inRange(hsv, lower, upper)
    result = img.copy()
    result[mask == 0] = (255, 255, 255)
    return result


# ============ 薪资文字解析 ============
def parse_salary(text):
    text = text.upper().replace(" ", "")
    # 先清理K后面可能的后缀，如 "14-17K·13薪" → "14-17K"
    text = re.sub(r'(K[×X]?\d*薪?.*)', 'K', text)
    match = re.search(r'(\d+)[~-](\d+)\s*K', text)
    if match:
        return float(match.group(1)), float(match.group(2))
    match = re.search(r'(\d+)\s*K', text)
    if match:
        v = float(match.group(1))
        return v, v
    return None


# ============ 薪资筛选 ============
def check_salary(salary_text, min_k, max_k):
    parsed = parse_salary(salary_text)
    if parsed is None:
        return False, f"薪资格式无法解析: {salary_text}"
    low, high = parsed
    if min_k is not None and high < min_k:
        return False, f"薪资{salary_text}最高{high}K < 最低要求{min_k}K"
    if max_k is not None and low > max_k:
        return False, f"薪资{salary_text}最低{low}K > 最高要求{max_k}K"
    return True, f"薪资{salary_text}符合要求"


# ============ 关键词过滤 ============
def check_keywords(all_text, include_words, exclude_words):
    # 去掉空格再匹配，避免OCR把"杭州"识别为"杭 州"导致匹配失败
    compact_text = all_text.replace(" ", "")
    if exclude_words:
        for word in exclude_words:
            if word.replace(" ", "") in compact_text:
                return False, f"包含排除词: {word}"
    if include_words:
        found = any(word.replace(" ", "") in compact_text for word in include_words)
        if not found:
            return False, f"不包含任何指定关键词: {include_words}"
    return True, "关键词符合"


# ============ OCR识别 ============
def ocr_recognize(image_input):
    """OCR识别，支持文件路径(str)或numpy数组(ndarray)"""
    ocr = _get_ocr()
    result, _ = ocr(image_input)
    texts = []
    if result:
        for item in result:
            box, text, confidence = item
            texts.append({"text": text, "confidence": confidence, "box": box})
    return texts


# ============ 信息提取 ============
def extract_info(all_texts, salary_texts):
    info = {"job": "", "salary": "", "city": "", "company": ""}
    if not salary_texts:
        return info
    salary_text = salary_texts[0]["text"]
    salary_box = salary_texts[0]["box"]
    info["salary"] = salary_text
    salary_y_center = (salary_box[0][1] + salary_box[2][1]) / 2
    salary_x_left = salary_box[0][0]

    # 岗位名称
    job_parts = []
    for t in all_texts:
        box = t["box"]
        y_center = (box[0][1] + box[2][1]) / 2
        x_right = box[1][0]
        if abs(y_center - salary_y_center) < 30 and x_right < salary_x_left:
            job_parts.append((x_right, t["text"]))
    job_parts.sort(key=lambda x: x[0])
    info["job"] = "".join([p[1] for p in job_parts])

    # 城市和公司
    below_texts = []
    for t in all_texts:
        box = t["box"]
        y_top = box[0][1]
        if y_top > salary_y_center + 20:
            below_texts.append((y_top, t["text"], box))
    below_texts.sort(key=lambda x: x[0])

    city_found = False
    company_found = False
    for _, text, box in below_texts:
        if not city_found:
            if "\u00b7" in text or "区" in text or "市" in text or "省" in text:
                info["city"] = text
                city_found = True
                continue
        if city_found and not company_found:
            if "\u00b7" in text:
                company = text.split("\u00b7")[0].strip()
                info["company"] = company
                company_found = True
                break
    return info


# ============ 投递操作 ============
class BossGUI:
    # 配置和日志保存到exe同级目录
    _DATA_DIR = WORK_DIR
    CONFIG_FILE = os.path.join(_DATA_DIR, "boss_config.json")
    LOG_DIR = os.path.join(_DATA_DIR, "投递日志")  # 日志文件夹
    APPLIED_FILE = os.path.join(_DATA_DIR, "已投递公司.json")  # 持久化已投递公司名
    BLACKLIST_FILE = os.path.join(_DATA_DIR, "黑名单公司.json")  # 黑名单公司

    def __init__(self, root):
        self.root = root
        self.root.title("BOSS直聘自动投递")
        self.root.geometry("850x650")
        self.root.resizable(True, True)
        self._stop_event = threading.Event()  # 线程安全的停止信号
        self._success_log_path = ""  # 当前投递成功日志路径
        self._fail_log_path = ""     # 当前投递失败日志路径
        self._json_lock = threading.Lock()  # JSON文件读写锁，防止异步并发覆盖
        self._blacklist_lock = threading.Lock()  # 黑名单读写锁，防止GUI线程与投递线程并发冲突
        self._blacklist = self._load_blacklist()  # 加载黑名单到内存

        self._build_ui()
        self._load_config()
        self._apply_platform()  # 根据配置初始化驱动
        self._preload_ocr()  # 后台预加载OCR模型
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        # ---- 设置区 ----
        frame_settings = ttk.LabelFrame(self.root, text="筛选设置（留空则不执行该条件）")
        frame_settings.pack(fill="x", padx=10, pady=5)

        # ---- 左列：筛选条件 ----
        left_col = ttk.Frame(frame_settings)
        left_col.pack(side="left", fill="both", expand=True, padx=(5, 5))

        # ---- 右列：操作参数 + 黑名单 ----
        right_col = ttk.Frame(frame_settings)
        right_col.pack(side="left", fill="both", expand=True, padx=(5, 5))

        # === 左列内容 ===
        # 平台选择
        row0 = ttk.Frame(left_col)
        row0.pack(fill="x", pady=3)
        ttk.Label(row0, text="手机平台:").pack(side="left")
        self.platform_var = tk.StringVar(value="android")
        platform_names = [label for _, label in PLATFORM_OPTIONS]
        self.platform_combo = ttk.Combobox(row0, textvariable=self.platform_var,
                                           values=platform_names, state="readonly", width=12)
        self.platform_combo.pack(side="left", padx=5)
        self.platform_combo.bind("<<ComboboxSelected>>", lambda e: self._apply_platform())

        # 薪资
        row1 = ttk.Frame(left_col)
        row1.pack(fill="x", pady=3)
        ttk.Label(row1, text="最低薪资(K):").pack(side="left")
        self.min_salary = ttk.Entry(row1, width=8)
        self.min_salary.pack(side="left", padx=5)
        ttk.Label(row1, text="最高薪资(K):").pack(side="left")
        self.max_salary = ttk.Entry(row1, width=8)
        self.max_salary.pack(side="left", padx=5)

        # 关键词
        row2 = ttk.Frame(left_col)
        row2.pack(fill="x", pady=3)
        ttk.Label(row2, text="包含关键词:").pack(side="left")
        self.include_kw = ttk.Entry(row2, width=20)
        self.include_kw.pack(side="left", padx=5)
        ttk.Label(row2, text="(逗号分隔)").pack(side="left")

        row3 = ttk.Frame(left_col)
        row3.pack(fill="x", pady=3)
        ttk.Label(row3, text="排除关键词:").pack(side="left")
        self.exclude_kw = ttk.Entry(row3, width=20)
        self.exclude_kw.pack(side="left", padx=5)
        ttk.Label(row3, text="(逗号分隔)").pack(side="left")

        # 防重复投递
        row_dedup = ttk.Frame(left_col)
        row_dedup.pack(fill="x", pady=3)
        self.dedup_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row_dedup, text="防重复投递（跳过已投递过的公司，重启后仍有效）", variable=self.dedup_var).pack(side="left")

        # === 右列内容 ===
        # 操作延迟
        row4 = ttk.Frame(right_col)
        row4.pack(fill="x", pady=3)
        ttk.Label(row4, text="操作延迟(毫秒):").pack(side="left")
        self.delay_entry = ttk.Entry(row4, width=8)
        self.delay_entry.insert(0, "3000")
        self.delay_entry.pack(side="left", padx=5)

        # 随机延迟
        row5 = ttk.Frame(right_col)
        row5.pack(fill="x", pady=3)
        ttk.Label(row5, text="随机延迟(毫秒):").pack(side="left")
        self.random_delay_entry = ttk.Entry(row5, width=8)
        self.random_delay_entry.insert(0, "50")
        self.random_delay_entry.pack(side="left", padx=5)
        ttk.Label(row5, text="(操作延迟 ± 随机延迟)").pack(side="left", padx=5)

        # 投递次数
        row6 = ttk.Frame(right_col)
        row6.pack(fill="x", pady=3)
        ttk.Label(row6, text="投递次数:").pack(side="left")
        self.deliver_count_entry = ttk.Entry(row6, width=8)
        self.deliver_count_entry.insert(0, "10")
        self.deliver_count_entry.pack(side="left", padx=5)

        # 黑名单
        row_black = ttk.Frame(right_col)
        row_black.pack(fill="x", pady=3)
        ttk.Label(row_black, text="黑名单公司:").pack(side="left")
        self.blacklist_entry = ttk.Entry(row_black, width=14)
        self.blacklist_entry.pack(side="left", padx=5)
        ttk.Button(row_black, text="添加", command=self._add_blacklist).pack(side="left", padx=2)
        ttk.Button(row_black, text="查看", command=self._view_blacklist).pack(side="left", padx=2)
        ttk.Button(row_black, text="清空", command=self._clear_blacklist).pack(side="left", padx=2)
        self.blacklist_count_var = tk.StringVar(value="")
        ttk.Label(row_black, textvariable=self.blacklist_count_var).pack(side="left", padx=5)

        # ---- 按钮 ----
        frame_btn = ttk.Frame(self.root)
        frame_btn.pack(fill="x", padx=10, pady=5)
        self.btn_run = ttk.Button(frame_btn, text="截图并识别", command=self.run)
        self.btn_run.pack(side="left", padx=5)
        self.btn_deliver = ttk.Button(frame_btn, text="开始投递", command=self.start_deliver)
        self.btn_deliver.pack(side="left", padx=5)
        self.btn_stop = ttk.Button(frame_btn, text="停止投递", command=self.stop_deliver, state="disabled")
        self.btn_stop.pack(side="left", padx=5)
        ttk.Button(frame_btn, text="清空日志", command=self._clear_log).pack(side="left", padx=5)
        ttk.Button(frame_btn, text="使用说明", command=self._show_help).pack(side="left", padx=5)

        # ---- 状态栏 ----
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(self.root, textvariable=self.status_var, relief="sunken", anchor="w").pack(fill="x", padx=10, pady=2)

        # ---- 结果区 ----
        frame_result = ttk.LabelFrame(self.root, text="识别结果")
        frame_result.pack(fill="both", expand=True, padx=10, pady=5)

        self.result_text = scrolledtext.ScrolledText(frame_result, wrap="word", font=("Consolas", 10))
        self.result_text.pack(fill="both", expand=True)

    def _apply_platform(self):
        """根据GUI下拉框选择切换设备驱动"""
        platform_label = self.platform_var.get()
        # 从显示标签反查平台ID
        platform_id = "android"
        for pid, plabel in PLATFORM_OPTIONS:
            if plabel == platform_label:
                platform_id = pid
                break
        driver = create_driver(platform_id)
        _set_driver(driver)
        tool_name = "ADB" if platform_id == "android" else "HDC"
        self._set_status(f"已切换到 {platform_label}，{tool_name}路径: {driver.adb_path if hasattr(driver, 'adb_path') else driver.hdc_path}")

    def log(self, msg):
        self.root.after(0, self._append_log, msg)

    def _clear_log(self):
        self.result_text.delete("1.0", "end")

    def _show_help(self):
        """弹出使用说明对话框"""
        help_win = tk.Toplevel(self.root)
        help_win.title("使用说明")
        help_win.geometry("620x580")
        help_win.resizable(True, True)

        help_text = scrolledtext.ScrolledText(help_win, wrap="word", font=("Microsoft YaHei UI", 10), padx=12, pady=8)
        help_text.pack(fill="both", expand=True, padx=5, pady=5)

        content = """\
一、平台选择
━━━━━━━━━━━━━━━━━━━━━━━━━━
[手机平台]  选择安卓或鸿蒙，切换后自动保存，下次启动恢复上次选择。
  安卓 (ADB)：通过ADB控制安卓手机，需开启USB调试。
  鸿蒙 (HDC)：通过HDC控制鸿蒙手机，需开启开发者模式。
  注意：纯血鸿蒙NEXT不支持ADB，必须选择鸿蒙(HDC)。

二、筛选设置
━━━━━━━━━━━━━━━━━━━━━━━━━━━
[最低薪资(K)]  填数字，如 15 表示最低15K。留空 = 不限制最低薪资。

[最高薪资(K)]  填数字，如 25 表示最高25K。留空 = 不限制最高薪资。
               只要招聘岗位的薪资范围与设置的区间有重叠就投递。
               例：设 15~25K → 岗位 14-18K(重叠)投递, 岗位 8-12K(无重叠)跳过。
               例：只填最低 20K → 岗位薪资上限 >=20K 就投递。

[包含关键词]  逗号分隔，通常用于筛选工作地点。
              岗位文字中必须包含至少一个才投递。
              例：北京,上海 → 含"北京"或"上海"的岗位才会投递。
              留空 = 不做关键词包含过滤。

[排除关键词]  逗号分隔，通常用于排除不想去的地点。
              岗位文字中含任一词则跳过。
              例：廊坊,燕郊 → 含这两个地点的岗位自动跳过。
              留空 = 不做关键词排除过滤。

[操作延迟(毫秒)]  每步操作后的等待时间，单位毫秒。
                  建议值 2000~5000。太快容易被风控。

[随机延迟(毫秒)]  在操作延迟基础上随机波动的范围。
                  实际延迟 = 操作延迟 +/- 随机延迟。
                  例：操作延迟3000 + 随机延迟50 → 实际 2950~3050ms。
                  让每次间隔略有不同，更像真人操作。

[投递次数]  目标投递成功数量（跳过的不算）。
            达到该数量后自动停止。

[防重复投递]  勾选后，同一公司只投递一次，重启程序后仍有效。
              已投递公司名保存在"已投递公司.json"中，可手动编辑删除。
              不勾选时，仅在本次运行期间去重（重启后记录丢失）。

三、功能按钮
━━━━━━━━━━━━━━━━━━━━━━━━━━━
[截图并识别]  对手机当前屏幕截图并OCR识别，显示岗位信息、
              薪资、关键词筛选结果。用于手动预览，不会投递。

[开始投递]  启动自动投递循环：
              识别 -> 关键词过滤 -> 薪资筛选 -> 去重检查
              -> 检测"立即沟通" -> 点击投递 -> 返回 -> 滑下一个，循环直到投递次数达标。

[停止投递]  手动停止投递循环，当前步骤完成后停止。

[清空日志]  清空下方识别结果文本区的内容。

四、自动防风控机制
━━━━━━━━━━━━━━━━━━━━━━━━━━━
- 每次点击带随机 +/-10 像素偏移
- 每次识别后 1/5 概率随机上滑一次
- 每投递10家随机休息 5~10 秒
- 延迟自带随机波动

五、异常自动恢复
━━━━━━━━━━━━━━━━━━━━━━━━━━━
- 截图失败：自动重试3次，仍失败则跳过当前岗位继续下一个
- OCR识别异常：自动重试2次，仍失败则跳过当前岗位
- 点击/滑动失败：自动重试2次，仍失败则跳过当前岗位
- 返回键异常：自动重试1次，仍失败则继续投递
- 识别流程整体异常：跳过当前岗位继续下一个
- 所有异常均记录日志，不会导致投递中断

六、自动停止条件
━━━━━━━━━━━━━━━━━━━━━━━━━━━
- 达到投递次数目标
- 连续3次识别到同一岗位（列表到底）
- 检测到弹窗关键词（投递上限/操作频繁/账号异常等）
- 手动点击"停止投递"

七、数据存储
━━━━━━━━━━━━━━━━━━━━━━━━━━
- 配置自动保存到 boss_config.json，下次启动自动恢复
- 投递日志按时间戳命名，保存在"投递日志"文件夹下：
  · XXXXXXXX_HHMMSS_投递成功日志.txt（仅记录投递成功的岗位）
  · XXXXXXXX_HHMMSS_投递失败日志.txt（记录跳过、重复等未投递的岗位）
- 每次执行投递自动创建新日志文件，历史日志保留不覆盖
- 已投递公司名保存在"已投递公司.json"，用于防重复投递
"""

        help_text.insert("1.0", content)
        help_text.config(state="disabled")  # 只读

        ttk.Button(help_win, text="关闭", command=help_win.destroy).pack(pady=8)

    # ============ 配置持久化 ============
    def _load_applied_companies(self):
        """从JSON文件加载已投递公司列表"""
        if not os.path.isfile(self.APPLIED_FILE):
            return set()
        try:
            with open(self.APPLIED_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return set(data) if isinstance(data, list) else set()
        except Exception:
            return set()

    def _save_applied_company(self, company):
        """异步追加保存一个已投递公司名到JSON文件"""
        if not company:
            return
        threading.Thread(target=self._write_applied_company, args=(company,), daemon=True).start()

    def _write_applied_company(self, company):
        """实际写已投递公司JSON（在子线程中执行，加锁防并发）"""
        with self._json_lock:
            try:
                if os.path.isfile(self.APPLIED_FILE):
                    with open(self.APPLIED_FILE, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if not isinstance(data, list):
                        data = []
                else:
                    data = []
                if company not in data:
                    data.append(company)
                    with open(self.APPLIED_FILE, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

    def _load_blacklist(self):
        """从JSON文件加载黑名单公司列表到内存"""
        if not os.path.isfile(self.BLACKLIST_FILE):
            return set()
        try:
            with open(self.BLACKLIST_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return set(data) if isinstance(data, list) else set()
        except Exception:
            return set()

    def _save_blacklist(self):
        """异步保存黑名单到JSON文件"""
        data = list(self._blacklist)
        threading.Thread(target=self._write_blacklist, args=(data,), daemon=True).start()

    def _write_blacklist(self, data):
        """实际写黑名单JSON（在子线程中执行，加锁）"""
        with self._json_lock:
            try:
                with open(self.BLACKLIST_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

    def _add_blacklist(self):
        """从输入框添加公司到黑名单"""
        name = self.blacklist_entry.get().strip()
        if not name:
            return
        with self._blacklist_lock:
            self._blacklist.add(name)
        self._save_blacklist()
        self.blacklist_entry.delete(0, "end")
        self._update_blacklist_count()
        self.log(f"[黑名单] 已添加: {name}")

    def _view_blacklist(self):
        """查看当前黑名单"""
        with self._blacklist_lock:
            bl_copy = sorted(self._blacklist)
        if not bl_copy:
            messagebox.showinfo("黑名单", "黑名单为空")
            return
        messagebox.showinfo("黑名单", f"共 {len(bl_copy)} 家公司:\n\n" + "\n".join(bl_copy))

    def _clear_blacklist(self):
        """清空黑名单"""
        with self._blacklist_lock:
            count = len(self._blacklist)
        if not count:
            return
        if messagebox.askyesno("清空黑名单", f"确定清空黑名单（共 {count} 家公司）？"):
            with self._blacklist_lock:
                self._blacklist.clear()
            self._save_blacklist()
            self._update_blacklist_count()
            self.log("[黑名单] 已清空")

    def _update_blacklist_count(self):
        """更新黑名单数量显示"""
        count = len(self._blacklist)
        self.blacklist_count_var.set(f"({count}家)" if count else "")

    def _on_close(self):
        self._save_config()
        self.root.destroy()

    def _preload_ocr(self):
        """后台预加载OCR模型，避免首次识别卡顿"""
        self._set_status("正在预加载OCR模型...")
        def _load():
            _get_ocr()
            self.root.after(0, lambda: self._set_status("就绪 (OCR已加载)"))
        threading.Thread(target=_load, daemon=True).start()

    def _save_config(self):
        cfg = {
            "min_salary": self.min_salary.get(),
            "max_salary": self.max_salary.get(),
            "include_kw": self.include_kw.get(),
            "exclude_kw": self.exclude_kw.get(),
            "delay": self.delay_entry.get(),
            "random_delay": self.random_delay_entry.get(),
            "deliver_count": self.deliver_count_entry.get(),
            "platform": self.platform_var.get(),
            "dedup": self.dedup_var.get(),
        }
        try:
            with open(self.CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _load_config(self):
        if not os.path.isfile(self.CONFIG_FILE):
            return
        try:
            with open(self.CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            mapping = {
                "min_salary": self.min_salary,
                "max_salary": self.max_salary,
                "include_kw": self.include_kw,
                "exclude_kw": self.exclude_kw,
                "delay": self.delay_entry,
                "random_delay": self.random_delay_entry,
                "deliver_count": self.deliver_count_entry,
            }
            for key, entry in mapping.items():
                if key in cfg:
                    val = str(cfg[key]) if cfg[key] is not None else ""
                    entry.delete(0, "end")
                    entry.insert(0, val)
            # 恢复平台选择
            if "platform" in cfg:
                self.platform_var.set(str(cfg["platform"]))
            # 恢复防重复投递开关
            if "dedup" in cfg:
                self.dedup_var.set(bool(cfg["dedup"]))
        except Exception:
            pass

    def _append_log(self, msg):
        self.result_text.insert("end", msg + "\n")
        self.result_text.see("end")

    def _set_status(self, msg):
        self.root.after(0, self.status_var.set, msg)

    def _set_running(self, running):
        if running:
            self.root.after(0, lambda: self.btn_run.config(state="disabled"))
            self.root.after(0, lambda: self.btn_deliver.config(state="disabled"))
            self.root.after(0, lambda: self.btn_stop.config(state="normal"))
        else:
            self.root.after(0, lambda: self.btn_run.config(state="normal"))
            self.root.after(0, lambda: self.btn_deliver.config(state="normal"))
            self.root.after(0, lambda: self.btn_stop.config(state="disabled"))

    # ============ 截图识别 ============
    def _parse_salary_inputs(self):
        """安全解析薪资输入，返回 (min_k, max_k)"""
        min_str = self.min_salary.get().strip()
        max_str = self.max_salary.get().strip()
        try:
            min_k = float(min_str) if min_str else None
        except ValueError:
            min_k = None
        try:
            max_k = float(max_str) if max_str else None
        except ValueError:
            max_k = None
        return min_k, max_k

    def _parse_keyword_inputs(self):
        """解析关键词输入，返回 (include_words, exclude_words)
        支持中英文逗号、空格分隔"""
        include_str = self.include_kw.get().strip()
        exclude_str = self.exclude_kw.get().strip()
        include_words = [w.strip() for w in re.split(r'[,，\s]+', include_str) if w.strip()] if include_str else []
        exclude_words = [w.strip() for w in re.split(r'[,，\s]+', exclude_str) if w.strip()] if exclude_str else []
        return include_words, exclude_words

    def run(self):
        self.result_text.delete("1.0", "end")
        # 设备预检查
        ok, msg = _device_check()
        if not ok:
            self.log(f"设备检查失败: {msg}")
            self._set_status(msg)
            return

        min_k, max_k = self._parse_salary_inputs()
        include_words, exclude_words = self._parse_keyword_inputs()

        self._set_running(True)
        t = threading.Thread(target=self._worker, args=(min_k, max_k, include_words, exclude_words), daemon=True)
        t.start()

    def _worker(self, min_k, max_k, include_words, exclude_words):
        try:
            info, skip_reason = self._do_recognize(min_k, max_k, include_words, exclude_words)
            if skip_reason:
                self._set_status(f"识别完成: {skip_reason}")
        except FileNotFoundError as e:
            self.log(f"文件错误: {e}")
            self._set_status(f"文件错误: {e}")
        except Exception as e:
            self.log(f"执行出错: {e}")
            self._set_status(f"出错: {e}")
        finally:
            self._set_running(False)

    def _safe_screenshot(self, max_retry=2):
        """安全截图，失败自动重试，返回 (ok, error_msg)
        注意：adb_screenshot内部已有3次重试，这里只额外重试2轮即可"""
        for i in range(max_retry):
            try:
                adb_screenshot()
                return True, ""
            except Exception as e:
                if i < max_retry - 1:
                    self.log(f"[异常恢复] 截图失败({i+1}/{max_retry}): {e}，重试中...")
                    time.sleep(2)
                else:
                    return False, f"截图失败({max_retry}次重试均失败): {e}"
        return False, "截图失败"

    def _safe_ocr(self, image_input, max_retry=2):
        """安全OCR识别，失败自动重试，返回 texts列表（空列表表示失败）"""
        for i in range(max_retry):
            try:
                texts = ocr_recognize(image_input)
                return texts
            except Exception as e:
                if i < max_retry - 1:
                    self.log(f"[异常恢复] OCR识别异常({i+1}/{max_retry}): {e}，重试中...")
                    time.sleep(1)
                else:
                    self.log(f"[异常恢复] OCR识别异常，跳过本次: {e}")
                    return []
        return []

    def _safe_tap(self, x, y, offset=10, max_retry=2):
        """安全点击，失败自动重试，返回 (ok, error_msg)"""
        for i in range(max_retry):
            try:
                adb_tap(x, y, offset)
                return True, ""
            except Exception as e:
                if i < max_retry - 1:
                    self.log(f"[异常恢复] 点击失败({i+1}/{max_retry}): {e}，重试中...")
                    time.sleep(1)
                else:
                    return False, f"点击失败: {e}"
        return False, "点击失败"

    def _safe_swipe(self, x1, y1, x2, y2, duration=500, max_retry=2):
        """安全滑动，失败自动重试，返回 (ok, error_msg)"""
        for i in range(max_retry):
            try:
                adb_swipe(x1, y1, x2, y2, duration)
                return True, ""
            except Exception as e:
                if i < max_retry - 1:
                    self.log(f"[异常恢复] 滑动失败({i+1}/{max_retry}): {e}，重试中...")
                    time.sleep(1)
                else:
                    return False, f"滑动失败: {e}"
        return False, "滑动失败"

    def _do_recognize(self, min_k, max_k, include_words, exclude_words):
        """识别流程，返回 (info dict, 跳过原因)，不符合返回 (None, 原因)"""
        # 1. 截图（异常自动恢复）
        self._set_status("正在截图...")
        self.log("正在截图...")
        ok, err = self._safe_screenshot()
        if not ok:
            return {"company": "", "job": "", "city": "", "salary": ""}, err

        # 2. 裁剪上半部分
        self._set_status("正在裁剪...")
        self.log("正在裁剪上半部分...")
        try:
            top_half = crop_top_half(SCREENSHOT_PATH)
        except Exception as e:
            self.log(f"[异常恢复] 裁剪失败: {e}")
            return {"company": "", "job": "", "city": "", "salary": ""}, f"裁剪失败: {e}"

        # 3. 全文OCR（异常自动恢复）
        self._set_status("正在OCR识别...")
        self.log("正在OCR识别全部文字...")
        all_texts = self._safe_ocr(top_half)
        all_text_str = " ".join([t["text"] for t in all_texts])
        self.log(f"识别到 {len(all_texts)} 条文字:")
        for t in all_texts:
            self.log(f"  {t['text']}  (置信度: {t['confidence']:.4f})")

        # 弹窗检测
        for kw in POPUP_KEYWORDS:
            if kw in all_text_str:
                self.log(f">>> 检测到弹窗关键词: {kw}，停止投递!")
                self._stop_event.set()
                return {"company": "", "job": "", "city": "", "salary": ""}, f"弹窗拦截: {kw}"

        # 4. 提取关键信息
        self._set_status("提取关键信息...")
        self.log("\n--- 提取关键信息 ---")
        try:
            salary_img_array = filter_green_color_img(top_half)
            salary_texts = self._safe_ocr(salary_img_array)
        except Exception as e:
            self.log(f"[异常恢复] 薪资识别异常: {e}")
            salary_texts = []
        info = extract_info(all_texts, salary_texts)
        self.log(f"  岗位: {info['job']}")
        self.log(f"  薪资: {info['salary']}")
        self.log(f"  城市: {info['city']}")
        self.log(f"  公司: {info['company']}")

        # 5. 关键词过滤
        if include_words or exclude_words:
            self._set_status("关键词过滤中...")
            self.log("\n--- 关键词过滤 ---")
            kw_ok, kw_msg = check_keywords(all_text_str, include_words, exclude_words)
            self.log(kw_msg)
            if not kw_ok:
                self.log(">>> 关键词不符，跳过该岗位")
                self._set_status("关键词不符，已跳过")
                return info, kw_msg
        else:
            self.log("\n--- 关键词过滤: 未设置，跳过 ---")

        # 6. 薪资筛选
        if min_k is not None or max_k is not None:
            self._set_status("薪资筛选中...")
            self.log("\n--- 薪资筛选 ---")
            if not salary_texts:
                self.log("未识别到薪资文字")
                self._set_status("未识别到薪资文字")
                return info, "未识别到薪资文字"
            salary_str = " ".join([t["text"] for t in salary_texts])
            self.log(f"薪资识别结果: {salary_str}")
            sal_ok, sal_msg = check_salary(salary_str, min_k, max_k)
            self.log(sal_msg)
            if not sal_ok:
                self.log(">>> 薪资不符，跳过该岗位")
                self._set_status("薪资不符，已跳过")
                return info, sal_msg
        else:
            self.log("\n--- 薪资筛选: 未设置，跳过 ---")

        self.log("\n========================================")
        self.log(">>> 所有条件均符合，可以投递!")
        self.log("========================================")
        self._set_status("识别完成，符合条件!")
        return info, ""

    def _safe_float(self, entry, default=0.0):
        """安全读取Entry中的浮点值，非数字返回默认值"""
        try:
            val = entry.get().strip()
            return float(val) if val else default
        except ValueError:
            return default

    def _safe_int(self, entry, default=0):
        """安全读取Entry中的整数值，非数字返回默认值"""
        try:
            val = entry.get().strip()
            return int(val) if val else default
        except ValueError:
            return default

    def _delay(self):
        """基础延迟(毫秒) ± 随机延迟(毫秒)"""
        base = self._safe_float(self.delay_entry, 3000)
        rand = self._safe_float(self.random_delay_entry, 50)
        ms = base + random.uniform(-rand, rand)
        time.sleep(max(ms, 0) / 1000)

    def _short_delay(self, default_sec=0.5):
        """短延迟：取操作延迟的1/3，但不超过default_sec秒，用于页面跳转/返回等短等待
        附加轻微随机波动（±50ms），防风控"""
        base_ms = self._safe_float(self.delay_entry, 3000)
        sec = min(base_ms / 1000 / 3, default_sec)
        jitter = random.uniform(-0.05, 0.05)  # ±50ms随机
        time.sleep(max(sec + jitter, 0.05))

    # ============ 自动投递 ============
    def start_deliver(self):
        self.result_text.delete("1.0", "end")
        # 设备预检查
        ok, msg = _device_check()
        if not ok:
            self.log(f"设备检查失败: {msg}")
            self._set_status(msg)
            messagebox.showwarning("设备检查失败", msg)
            return

        min_k, max_k = self._parse_salary_inputs()
        include_words, exclude_words = self._parse_keyword_inputs()

        self._stop_event.clear()
        self._init_log_files()  # 创建本次投递的日志文件
        self._set_running(True)
        t = threading.Thread(target=self._deliver_worker, args=(min_k, max_k, include_words, exclude_words), daemon=True)
        t.start()

    def stop_deliver(self):
        self._stop_event.set()
        self.log(">>> 正在停止投递...")
        self._set_status("正在停止...")

    def _init_log_files(self):
        """每次执行投递时创建新的日志文件（时间戳命名）"""
        now = datetime.datetime.now()
        timestamp = now.strftime("%Y%m%d_%H%M%S")
        # 确保日志目录存在
        os.makedirs(self.LOG_DIR, exist_ok=True)
        self._success_log_path = os.path.join(self.LOG_DIR, f"{timestamp}_投递成功日志.txt")
        self._fail_log_path = os.path.join(self.LOG_DIR, f"{timestamp}_投递失败日志.txt")
        # 写入表头
        header = (f"{'公司':<22}│  {'岗位':<22}│  {'城市':<18}│  {'薪资':<14}│  {'结果':<10}")
        sep = "─" * 95
        for path in [self._success_log_path, self._fail_log_path]:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(f"{sep}\n{header}\n{sep}\n")
            except Exception:
                pass

    def _save_deliver_log(self, info, result, reason=""):
        """异步保存投递日志，投递成功和失败分别写入不同文件"""
        now = datetime.datetime.now()
        timestamp = now.strftime("%Y/%m/%d %H:%M:%S")

        def _pad(text, width):
            """中英文混合固定宽度对齐，中文算2宽度"""
            text = str(text)
            w = sum(2 if ord(c) > 127 else 1 for c in text)
            return text + " " * max(0, width - w)

        # 结果标签美化
        if result == "投递":
            result_tag = "✓ 投递"
        elif result == "跳过":
            result_tag = "✗ 跳过"
        elif result == "重复":
            result_tag = "↻ 重复"
        else:
            result_tag = f"  {result}"

        line = (f"[{timestamp}] "
                f"{_pad(info.get('company', ''), 20)}  │  "
                f"{_pad(info.get('job', ''), 20)}  │  "
                f"{_pad(info.get('city', ''), 16)}  │  "
                f"{_pad(info.get('salary', ''), 12)}  │  "
                f"{_pad(result_tag, 8)}")
        if reason:
            line += f" → {reason}"

        # 根据结果类型选择写入成功或失败日志
        if result == "投递":
            log_path = self._success_log_path
        else:
            log_path = self._fail_log_path
        threading.Thread(target=self._write_log, args=(log_path, line), daemon=True).start()

    def _write_log(self, log_path, line):
        """实际写日志文件（在子线程中执行）"""
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line.rstrip() + "\n")
        except Exception:
            pass

    def _random_up_swipe(self):
        """1/5概率随机上滑，防风控"""
        if random.randint(1, 5) == 1:
            sw, sh = adb_get_screen_size()
            y = max(100, sh * 2 // 3)
            if sw > 200:
                x1 = random.randint(100, sw - 100)
                x2 = random.randint(100, sw - 100)
            else:
                x1 = x2 = sw // 2
            y2 = max(50, y - 150 + random.randint(-10, 10))
            self.log("[防风控] 随机上滑一次")
            self._safe_swipe(x1, y, x2, y2, duration=500)

    def _deliver_worker(self, min_k, max_k, include_words, exclude_words):
        """自动投递子线程"""
        try:
            deliver_count = 0  # 实际投递计数（跳过不算）
            skip_count = 0     # 跳过计数
            dup_count = 0      # 重复跳过计数
            scan_count = 0     # 扫描计数
            max_count = self._safe_int(self.deliver_count_entry, 15)
            start_time = time.time()
            delivered_companies = set()  # 本次会话已投递公司名去重
            # 如果开启了防重复投递，加载历史已投递公司
            if self.dedup_var.get():
                loaded = self._load_applied_companies()
                delivered_companies.update(loaded)
                if loaded:
                    self.log(f"[防重复投递] 已加载 {len(loaded)} 家历史已投递公司")
            # 加载黑名单（复制时加锁防并发）
            with self._blacklist_lock:
                blacklist = set(self._blacklist)
            if blacklist:
                self.log(f"[黑名单] 已加载 {len(blacklist)} 家黑名单公司")
            last_job_key = ""            # 上一次岗位标识（公司+岗位），用于检测横滑到底
            same_page_count = 0         # 连续相同岗位计数
            MAX_SAME_PAGE = 3           # 连续N次相同认为已到列表底部

            def _update_status(extra=""):
                elapsed = int(time.time() - start_time)
                m, s = divmod(elapsed, 60)
                self._set_status(
                    f"投递{deliver_count} 跳过{skip_count} 重复{dup_count} 共{scan_count} "
                    f"用时{m}分{s}秒 {extra}"
                )

            while not self._stop_event.is_set() and deliver_count < max_count:
                scan_count += 1
                self.log(f"\n{'='*40}")
                self.log(f"第 {scan_count} 次扫描 (已投递 {deliver_count}/{max_count})")
                self.log(f"{'='*40}")

                # 1. 识别筛选
                _update_status(f"扫描第{scan_count}个 - 识别中...")
                try:
                    info, skip_reason = self._do_recognize(min_k, max_k, include_words, exclude_words)
                except Exception as e:
                    # 识别整体异常时，跳过本次继续下一个
                    self.log(f"[异常恢复] 识别流程异常: {e}，跳过本次")
                    skip_count += 1
                    self._swipe_next()
                    _update_status(f"识别异常，跳过")
                    continue
                self._random_up_swipe()  # 识别完成后1/5概率随机上滑防风控

                # ---- 横滑到底检测 ----
                current_job_key = f"{info.get('company', '')}|{info.get('job', '')}"
                if current_job_key == last_job_key and current_job_key != "|":
                    same_page_count += 1
                    if same_page_count >= MAX_SAME_PAGE:
                        self.log(f">>> 连续{MAX_SAME_PAGE}次扫描到相同岗位，已到列表底部，停止投递")
                        break
                else:
                    same_page_count = 0
                last_job_key = current_job_key

                # ---- 已投递去重 ----
                company = info.get("company", "")
                # 黑名单检查（始终生效）
                if company and company in blacklist:
                    skip_count += 1
                    self.log(f">>> 公司 [{company}] 在黑名单中，跳过")
                    self._save_deliver_log(info, "跳过", f"黑名单: {company}")
                    self._swipe_next()
                    _update_status(f"黑名单: {company[:15]}")
                    continue
                if company and company in delivered_companies:
                    dup_count += 1
                    self.log(f">>> 公司 [{company}] 已投递过，跳过重复")
                    self._save_deliver_log(info, "重复", f"已投递过: {company}")
                    self._swipe_next()
                    _update_status(f"重复: {company[:15]}")
                    continue

                if skip_reason:
                    skip_count += 1
                    self.log("不符合条件，跳过，滑动下一个...")
                    self._save_deliver_log(info, "跳过", skip_reason)
                    self._swipe_next()
                    _update_status("跳过: " + skip_reason[:20])
                    continue

                # 4. 检测底部按钮（下方1/4区域）
                _update_status(f"扫描第{scan_count}个 - 检测沟通按钮...")
                self.log("\n--- 步骤4: 检测沟通按钮 ---")
                # 重新截图，确保获取当前屏幕状态（异常自动恢复）
                ok, err = self._safe_screenshot()
                if not ok:
                    self.log(f"[异常恢复] {err}，跳过本次")
                    skip_count += 1
                    self._save_deliver_log(info, "跳过", err)
                    self._swipe_next()
                    _update_status("截图失败")
                    continue

                # 先检测"继续沟通"（说明已沟通过）
                continue_pos = self._find_button_in_screen("继续沟通", 0.75, 1.0)
                if continue_pos is not None:
                    self.log("检测到\"继续沟通\"，已沟通过，跳过")
                    dup_count += 1
                    # 加入去重集，避免同一会话反复遇到
                    if company:
                        delivered_companies.add(company)
                        # 始终持久化保存
                        self._save_applied_company(company)
                    self._save_deliver_log(info, "重复", "已沟通过")
                    self._swipe_next()
                    _update_status("已沟通过")
                    continue

                # 再检测"立即沟通"
                btn_pos = self._find_button_in_screen("立即沟通", 0.75, 1.0)
                if btn_pos is None:
                    self.log("未找到\"立即沟通\"按钮，跳过")
                    skip_count += 1
                    self._save_deliver_log(info, "跳过", "未找到立即沟通按钮")
                    self._swipe_next()
                    _update_status("未找到立即沟通")
                    continue
                self.log(f"找到\"立即沟通\"坐标: ({btn_pos[0]}, {btn_pos[1]})")
                # 点击（异常自动恢复）
                tap_ok, tap_err = self._safe_tap(btn_pos[0], btn_pos[1])
                if not tap_ok:
                    self.log(f"[异常恢复] {tap_err}，跳过本次")
                    skip_count += 1
                    self._save_deliver_log(info, "跳过", tap_err)
                    self._swipe_next()
                    _update_status("点击失败")
                    continue
                self._short_delay(1.0)  # 等页面跳转

                # 3. 点击后验证：重新截图检测是否仍在原页面（立即沟通还在=点击无效）
                _update_status(f"扫描第{scan_count}个 - 验证点击结果...")
                self.log("\n--- 步骤3: 验证点击是否生效 ---")
                verify_ok, _ = self._safe_screenshot()
                click_effective = True
                if verify_ok:
                    # 检测"继续沟通"（如果出现说明已沟通过，计为重复）
                    after_continue = self._find_button_in_screen("继续沟通", 0.75, 1.0)
                    if after_continue is not None:
                        self.log("点击后检测到\"继续沟通\"，该岗位已沟通过")
                        dup_count += 1
                        if company:
                            delivered_companies.add(company)
                            self._save_applied_company(company)
                        self._save_deliver_log(info, "重复", "已沟通过")
                        # 返回原页面
                        try:
                            self._press_back()
                        except Exception:
                            pass
                        self._short_delay(0.5)
                        self._swipe_next()
                        _update_status("已沟通过")
                        continue
                    # 再次检测"立即沟通"，如果还在说明点击没生效
                    after_btn = self._find_button_in_screen("立即沟通", 0.75, 1.0)
                    if after_btn is not None:
                        self.log("点击后仍检测到\"立即沟通\"，点击可能未生效")
                        click_effective = False

                if not click_effective:
                    skip_count += 1
                    self._save_deliver_log(info, "跳过", "点击立即沟通无效")
                    self._swipe_next()
                    _update_status("点击无效")
                    continue

                # 4. 返回
                _update_status(f"扫描第{scan_count}个 - 返回...")
                self.log("\n--- 步骤4: 返回 ---")
                try:
                    self._press_back()
                except Exception as e:
                    self.log(f"[异常恢复] 返回键异常: {e}，重试一次...")
                    try:
                        time.sleep(1)
                        self._press_back()
                    except Exception:
                        self.log("[异常恢复] 返回键重试仍失败，继续投递")
                self._short_delay(0.5)  # 返回动画很短

                # 5. 横滑切换下一个岗位（异常自动恢复）
                _update_status(f"扫描第{scan_count}个 - 滑动下一个...")
                self.log("\n--- 步骤5: 滑动切换下一个岗位 ---")
                self._swipe_next()
                self._wait_page_stable()

                deliver_count += 1
                if company:
                    delivered_companies.add(company)
                    # 始终持久化保存，无论是否开启防重复投递
                    self._save_applied_company(company)
                self.log(f">>> 已投递 {deliver_count} 个岗位!")
                self._save_deliver_log(info, "投递")
                _update_status()

                # 每投递10个随机休息5-10秒
                if deliver_count % 10 == 0:
                    rest = random.randint(5, 10)
                    self.log(f"已投递 {deliver_count} 家公司，稍等 {rest} 秒...")
                    _update_status(f"休息{rest}秒...")
                    time.sleep(rest)

                # 用户操作延迟（每轮投递结束后等待）
                self._delay()

            # ---- 投递结束汇总 ----
            total_elapsed = int(time.time() - start_time)
            m, s = divmod(total_elapsed, 60)
            summary = (
                f"\n{'='*40}\n"
                f"投递结束汇总\n"
                f"{'='*40}\n"
                f"投递成功: {deliver_count}\n"
                f"跳过: {skip_count}\n"
                f"重复去重: {dup_count}\n"
                f"总计扫描: {scan_count}\n"
                f"用时: {m}分{s}秒\n"
                f"{'='*40}"
            )
            self.log(summary)
            if self._stop_event.is_set():
                self._set_status(f"已手动停止 | 投递{deliver_count} 跳过{skip_count} 用时{m}分{s}秒")
            elif same_page_count >= MAX_SAME_PAGE:
                self._set_status(f"已到列表底部 | 投递{deliver_count} 跳过{skip_count} 用时{m}分{s}秒")
            elif deliver_count >= max_count:
                self._set_status(f"投递完成! | 投递{deliver_count} 跳过{skip_count} 用时{m}分{s}秒")
            else:
                self._set_status(f"投递结束 | 投递{deliver_count} 跳过{skip_count} 用时{m}分{s}秒")

        except Exception as e:
            self.log(f"投递出错: {e}")
            self._set_status(f"出错: {e}")
        finally:
            self._set_running(False)

    def _find_button_in_screen(self, keyword, y_start_ratio, y_end_ratio, x_start_ratio=0, x_end_ratio=1):
        """在全屏截图中指定区域找按钮（内存版，不写中间文件）
        优先: HSV绿色过滤+OCR识别 -> 备用: 直接OCR识别底部区域"""
        img = cv2_imread(SCREENSHOT_PATH)
        if img is None:
            self.log(f"[警告] 截图文件读取失败，无法查找按钮: {keyword}")
            return None
        h, w = img.shape[:2]
        # 裁剪区域
        region = img[int(h * y_start_ratio):int(h * y_end_ratio),
                     int(w * x_start_ratio):int(w * x_end_ratio)]

        # 方式1: HSV绿色过滤 + OCR
        green_img = filter_green_color_img(region)
        texts = self._safe_ocr(green_img)
        for t in texts:
            if keyword in t["text"]:
                box = t["box"]
                cx = int((box[0][0] + box[2][0]) / 2 + w * x_start_ratio)
                cy = int((box[0][1] + box[2][1]) / 2 + h * y_start_ratio)
                return (cx, cy)

        # 方式2: 直接对原图OCR（不依赖HSV颜色过滤）
        texts = self._safe_ocr(region)
        for t in texts:
            if keyword in t["text"]:
                box = t["box"]
                cx = int((box[0][0] + box[2][0]) / 2 + w * x_start_ratio)
                cy = int((box[0][1] + box[2][1]) / 2 + h * y_start_ratio)
                return (cx, cy)

        return None

    def _press_back(self):
        """返回键"""
        if _driver:
            _driver.press_back()

    def _swipe_next(self):
        """向左滑动切换下一个岗位：X=屏幕宽-随机150~100 → X=200+随机0~100，Y=2/3高度±30
        滑动后等待2秒再截图，确保页面完全加载
        注意：终点x2需≥200，避开安卓左侧边缘返回手势区域"""
        sw, sh = adb_get_screen_size()
        y = max(100, sh * 2 // 3 + random.randint(-30, 30))
        x1 = max(100, sw - random.randint(100, 150))           # 起点：屏幕右侧
        x2 = 200 + random.randint(0, 100)                        # 终点：200~300，避开边缘手势
        self._safe_swipe(x1, y, x2, y, duration=500)
        time.sleep(2)  # 滑动结束等待2秒再截图

    def _wait_page_stable(self, max_wait=2.0, interval=0.5):
        """滑动后等待页面加载稳定：先短暂延迟，再截一次图做简单像素均值对比
        比全量blockMeanHash轻量，且不依赖opencv-contrib"""
        time.sleep(interval)  # 先等一个基础间隔让动画开始
        elapsed = interval
        prev_mean = None
        while elapsed < max_wait:
            try:
                ok, _ = self._safe_screenshot()
                if not ok:
                    time.sleep(interval)
                    elapsed += interval
                    continue
                img = cv2_imread(SCREENSHOT_PATH)
                if img is None:
                    time.sleep(interval)
                    elapsed += interval
                    continue
                curr_mean = img.mean()
                if prev_mean is not None and abs(curr_mean - prev_mean) < 0.5:
                    return  # 均值变化极小，页面稳定
                prev_mean = curr_mean
            except Exception:
                pass
            time.sleep(interval)
            elapsed += interval


def main():
    root = tk.Tk()
    app = BossGUI(root)
    root.mainloop()

if __name__ == "__main__":
    main()
