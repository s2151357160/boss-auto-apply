# device_driver.py
# 功能：设备驱动抽象层，支持安卓(ADB)和鸿蒙(HDC)

import os
import sys
import re
import subprocess
import time
import random

# ============ 常量 ============
SUBPROC_FLAGS = 0x08000000  # CREATE_NO_WINDOW，隐藏命令行窗口
TIMEOUT = 15
SHORT_TIMEOUT = 5

# ============ 驱动基类 ============
class DeviceDriver:
    """设备驱动基类，定义统一接口"""

    name = "base"

    def check_connection(self):
        """检查设备连接，返回 (ok, msg)"""
        raise NotImplementedError

    def screenshot(self, save_path):
        """截图并保存到本地路径，返回 (ok, msg)"""
        raise NotImplementedError

    def tap(self, x, y, offset=10):
        """点击坐标，带随机偏移"""
        raise NotImplementedError

    def swipe(self, x1, y1, x2, y2, duration=300):
        """滑动操作"""
        raise NotImplementedError

    def get_screen_size(self):
        """获取屏幕分辨率，返回 (width, height)"""
        raise NotImplementedError

    def press_back(self):
        """按返回键"""
        raise NotImplementedError

    def kill_server(self):
        """重启服务（截图失败时用）"""
        pass

    def start_server(self):
        """启动服务"""
        pass


# ============ 安卓 ADB 驱动 ============
class ADBDriver(DeviceDriver):
    """安卓设备驱动，基于 ADB 命令"""

    name = "android"

    def __init__(self, adb_path):
        self.adb_path = adb_path
        self._screen_size = None

    def _run(self, args, timeout=SHORT_TIMEOUT, check=False, capture=False):
        """执行ADB命令的统一封装"""
        cmd = [self.adb_path] + args
        kwargs = dict(timeout=timeout, creationflags=SUBPROC_FLAGS)
        if capture:
            kwargs["capture_output"] = True
            kwargs["text"] = True
        if check:
            kwargs["check"] = True
        return subprocess.run(cmd, **kwargs)

    def check_connection(self):
        """检查ADB是否可用，返回 (ok, msg)"""
        if not os.path.isfile(self.adb_path):
            return False, f"ADB未找到: {self.adb_path}"
        try:
            r = self._run(["devices"], capture=True)
            devices = []
            for line in r.stdout.strip().split("\n")[1:]:
                line = line.strip()
                if line and "device" in line and "unauthorized" not in line and "daemon" not in line:
                    devices.append(line.split("\t")[0])
            if not devices:
                return False, "未检测到手机连接，请检查USB线和USB调试"
            return True, f"已连接: {devices[0]}"
        except subprocess.TimeoutExpired:
            return False, "ADB命令超时，请重启ADB或重新插拔USB"
        except Exception as e:
            return False, f"ADB检查失败: {e}"

    def screenshot(self, save_path, max_retry=3):
        """ADB截图，失败自动重试"""
        for i in range(max_retry):
            try:
                self._run(["shell", "screencap", "-p", "/sdcard/screenshot.png"], timeout=TIMEOUT, check=True)
                self._run(["pull", "/sdcard/screenshot.png", save_path], timeout=TIMEOUT, check=True)
                self._run(["shell", "rm", "/sdcard/screenshot.png"])
                return True, "截图成功"
            except subprocess.TimeoutExpired:
                if i < max_retry - 1:
                    time.sleep(1)
                    self.kill_server()
                    self.start_server()
                    time.sleep(2)
                else:
                    return False, f"ADB截图超时({max_retry}次重试均失败)，请检查USB连接"
            except subprocess.CalledProcessError as e:
                if i < max_retry - 1:
                    time.sleep(1)
                    self.kill_server()
                    self.start_server()
                    time.sleep(2)
                else:
                    return False, f"ADB截图失败({max_retry}次重试均失败): {e}"
        return False, "截图失败"

    def tap(self, x, y, offset=10):
        """随机偏移点击"""
        rx = max(0, x + random.randint(-offset, offset))
        ry = max(0, y + random.randint(-offset, offset))
        self._run(["shell", "input", "tap", str(rx), str(ry)])

    def swipe(self, x1, y1, x2, y2, duration=300):
        """滑动操作"""
        self._run(["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration)])

    def get_screen_size(self):
        """获取屏幕分辨率（缓存结果）"""
        if self._screen_size is not None:
            return self._screen_size
        try:
            result = self._run(["shell", "wm", "size"], capture=True)
            match = re.search(r'(\d+)x(\d+)', result.stdout)
            if match:
                self._screen_size = (int(match.group(1)), int(match.group(2)))
        except Exception:
            pass
        if self._screen_size is None:
            self._screen_size = (1080, 2400)
        return self._screen_size

    def press_back(self):
        """ADB返回键"""
        try:
            self._run(["shell", "input", "keyevent", "4"])
        except Exception:
            pass

    def kill_server(self):
        """关闭ADB服务"""
        try:
            self._run(["kill-server"])
        except Exception:
            pass

    def start_server(self):
        """启动ADB服务"""
        try:
            self._run(["start-server"])
        except Exception:
            pass


# ============ 鸿蒙 HDC 驱动 ============
class HDCDriver(DeviceDriver):
    """鸿蒙设备驱动，基于 HDC 命令"""

    name = "harmony"

    def __init__(self, hdc_path):
        self.hdc_path = hdc_path
        self._screen_size = None

    def _run(self, args, timeout=SHORT_TIMEOUT, check=False, capture=False):
        """执行HDC命令的统一封装"""
        cmd = [self.hdc_path] + args
        kwargs = dict(timeout=timeout, creationflags=SUBPROC_FLAGS)
        if capture:
            kwargs["capture_output"] = True
            kwargs["text"] = True
        if check:
            kwargs["check"] = True
        return subprocess.run(cmd, **kwargs)

    def check_connection(self):
        """检查HDC是否可用，返回 (ok, msg)"""
        if not os.path.isfile(self.hdc_path):
            return False, f"HDC未找到: {self.hdc_path}"
        try:
            r = self._run(["list", "targets"], capture=True)
            lines = r.stdout.strip().split("\n")
            devices = []
            for line in lines:
                line = line.strip()
                # 过滤空行和 "[Empty]" 提示
                if line and line != "[Empty]" and not line.startswith("["):
                    devices.append(line)
            if not devices:
                return False, "未检测到鸿蒙设备连接，请检查USB线和开发者模式"
            return True, f"已连接: {devices[0]}"
        except subprocess.TimeoutExpired:
            return False, "HDC命令超时，请检查HDC和USB连接"
        except Exception as e:
            return False, f"HDC检查失败: {e}"

    def screenshot(self, save_path, max_retry=3):
        """HDC截图，失败自动重试
        鸿蒙截图命令：hdc shell snapshot_display -f /data/local/tmp/screenshot.jpeg
        注意：鸿蒙 snapshot_display 只支持 .jpeg 后缀，不能用 .png
        拉取命令：hdc file recv /data/local/tmp/screenshot.jpeg local_path"""
        remote_path = "/data/local/tmp/screenshot.jpeg"
        for i in range(max_retry):
            try:
                self._run(["shell", "snapshot_display", "-f", remote_path],
                          timeout=TIMEOUT, check=True)
                self._run(["file", "recv", remote_path, save_path],
                          timeout=TIMEOUT, check=True)
                # 清理手机端截图
                try:
                    self._run(["shell", "rm", remote_path])
                except Exception:
                    pass
                return True, "截图成功"
            except subprocess.TimeoutExpired:
                if i < max_retry - 1:
                    time.sleep(1)
                    self.kill_server()
                    self.start_server()
                    time.sleep(2)
                else:
                    return False, f"HDC截图超时({max_retry}次重试均失败)，请检查USB连接"
            except subprocess.CalledProcessError as e:
                if i < max_retry - 1:
                    time.sleep(1)
                    self.kill_server()
                    self.start_server()
                    time.sleep(2)
                else:
                    return False, f"HDC截图失败({max_retry}次重试均失败): {e}"
        return False, "截图失败"

    def tap(self, x, y, offset=10):
        """随机偏移点击
        鸿蒙点击命令：hdc shell uitest uiInput click x y"""
        rx = max(0, x + random.randint(-offset, offset))
        ry = max(0, y + random.randint(-offset, offset))
        self._run(["shell", "uitest", "uiInput", "click", str(rx), str(ry)])

    def swipe(self, x1, y1, x2, y2, duration=300):
        """滑动操作
        鸿蒙滑动命令：hdc shell uitest uiInput swipe x1 y1 x2 y2 [velocity]
        velocity范围200-40000，默认600"""
        # duration(ms) -> velocity: 大约 600 = 300ms, 越快velocity越大
        velocity = max(200, min(40000, int(600 * 300 / max(duration, 50))))
        self._run(["shell", "uitest", "uiInput", "swipe", str(x1), str(y1), str(x2), str(y2), str(velocity)])

    def get_screen_size(self):
        """获取屏幕分辨率（缓存结果）
        鸿蒙命令：hdc shell wm size"""
        if self._screen_size is not None:
            return self._screen_size
        try:
            result = self._run(["shell", "wm", "size"], capture=True)
            match = re.search(r'(\d+)x(\d+)', result.stdout)
            if match:
                self._screen_size = (int(match.group(1)), int(match.group(2)))
        except Exception:
            pass
        if self._screen_size is None:
            self._screen_size = (1080, 2400)
        return self._screen_size

    def press_back(self):
        """HDC返回键
        鸿蒙返回键：hdc shell uitest uiInput keyEvent Back"""
        try:
            self._run(["shell", "uitest", "uiInput", "keyEvent", "Back"])
        except Exception:
            # 备用：input keyevent 4
            try:
                self._run(["shell", "input", "keyevent", "4"])
            except Exception:
                pass

    def kill_server(self):
        """关闭HDC服务"""
        try:
            self._run(["kill"])
        except Exception:
            pass

    def start_server(self):
        """启动HDC服务（HDC通常自动启动，无需手动）"""
        pass


# ============ 驱动工厂 ============
def create_driver(platform, tool_dir=None):
    """根据平台创建驱动实例
    platform: 'android' 或 'harmony'
    tool_dir: 平台工具所在目录，None则自动查找"""
    if platform == "android":
        adb_path = _find_tool("adb.exe", "platform-tools", tool_dir)
        return ADBDriver(adb_path)
    elif platform == "harmony":
        hdc_path = _find_tool("hdc.exe", "hdc", tool_dir)
        return HDCDriver(hdc_path)
    else:
        raise ValueError(f"不支持的平台: {platform}")


def _find_tool(exe_name, sub_dir, tool_dir=None):
    """查找工具路径，优先级：_MEIPASS > tool_dir > WORK_DIR > 脚本目录"""
    # 1. PyInstaller 打包模式：从 _MEIPASS 临时目录查找
    _meipass = getattr(sys, '_MEIPASS', '')
    if _meipass:
        path = os.path.join(_meipass, sub_dir, exe_name)
        if os.path.isfile(path):
            return path

    # 2. 指定的工具目录
    if tool_dir:
        path = os.path.join(tool_dir, sub_dir, exe_name)
        if os.path.isfile(path):
            return path
        path = os.path.join(tool_dir, exe_name)
        if os.path.isfile(path):
            return path

    # 3. WORK_DIR（exe同级目录或脚本同级目录）
    if getattr(sys, 'frozen', False):
        work_dir = os.path.dirname(sys.executable)
    else:
        work_dir = os.path.dirname(os.path.abspath(__file__))

    path = os.path.join(work_dir, sub_dir, exe_name)
    if os.path.isfile(path):
        return path

    # 4. 直接在当前目录下
    path = os.path.join(work_dir, exe_name)
    if os.path.isfile(path):
        return path

    # 5. 未找到，返回默认路径（后续 check_connection 会报具体错误）
    return os.path.join(work_dir, sub_dir, exe_name)


# ============ 平台选项 ============
PLATFORM_OPTIONS = [
    ("android", "安卓 (ADB)"),
    ("harmony", "鸿蒙 (HDC)"),
]
