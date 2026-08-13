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
        """检查ADB是否可用，返回 (ok, msg)
        ADB冷启动和USB设备枚举可能需要数秒，持续轮询而不是只检查固定两次。"""
        if not os.path.isfile(self.adb_path):
            return False, f"ADB未找到: {self.adb_path}"

        # start-server 在服务已运行时会立即返回；冷启动时可能超过5秒。
        self.start_server()
        ok, msg = self._wait_for_device(timeout=10)
        if ok:
            return ok, msg

        # 手机已经出现但尚未授权时，重启服务无助于解决问题，直接给出准确提示。
        if "未授权" in msg:
            return False, msg

        # 服务可能卡死或设备处于offline，重启ADB后留足时间重新枚举USB设备。
        self.kill_server()
        time.sleep(0.5)
        self.start_server()
        ok, msg = self._wait_for_device(timeout=15)
        if ok:
            return ok, msg

        return False, msg

    def _wait_for_device(self, timeout=10, interval=0.5):
        """在限定时间内等待ADB完成服务启动和USB设备枚举。"""
        deadline = time.monotonic() + timeout
        last_msg = "未检测到手机"
        while time.monotonic() < deadline:
            ok, last_msg = self._check_devices()
            if ok:
                return True, last_msg
            if "未授权" in last_msg:
                return False, last_msg
            time.sleep(interval)
        return False, last_msg

    def _check_devices(self):
        """内部检测设备列表"""
        try:
            # ADB首次启动daemon通常需要5秒以上，不能使用5秒短超时。
            r = self._run(["devices"], timeout=TIMEOUT, capture=True)
            states = []
            for line in r.stdout.strip().split("\n")[1:]:
                line = line.strip()
                if not line or line.startswith("*"):
                    continue
                parts = line.split()
                if len(parts) >= 2:
                    states.append((parts[0], parts[1]))
            connected = [serial for serial, state in states if state == "device"]
            if connected:
                return True, f"已连接: {connected[0]}"
            unauthorized = [serial for serial, state in states if state == "unauthorized"]
            if unauthorized:
                return False, "已检测到手机但USB调试未授权，请解锁手机并点击“允许”"
            offline = [serial for serial, state in states if state == "offline"]
            if offline:
                return False, "已检测到手机但设备处于offline，正在尝试重连"
            return False, "未检测到手机连接，请检查USB连接模式、数据线和USB调试授权"
        except subprocess.TimeoutExpired:
            return False, "ADB命令超时"
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

    def swipe(self, x1, y1, x2, y2, duration=500):
        """滑动操作，duration最低500ms兼容更多ROM"""
        duration = max(duration, 500)
        self._run(["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration)])

    def get_screen_size(self):
        """获取屏幕分辨率（缓存结果）
        优先使用Override size（用户实际使用的分辨率），
        因为 input swipe/tap 命令使用的是Override坐标系而非物理分辨率"""
        if self._screen_size is not None:
            return self._screen_size
        try:
            result = self._run(["shell", "wm", "size"], capture=True)
            text = result.stdout
            # 优先取 Override size（如 1080x2400），这才是 input 命令的坐标系
            override_match = re.search(r'Override\s+size:\s*(\d+)x(\d+)', text)
            if override_match:
                self._screen_size = (int(override_match.group(1)), int(override_match.group(2)))
            else:
                # 没有 Override，取 Physical size
                phys_match = re.search(r'Physical\s+size:\s*(\d+)x(\d+)', text)
                if phys_match:
                    self._screen_size = (int(phys_match.group(1)), int(phys_match.group(2)))
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
            # 冷启动daemon实测可能超过5秒，使用完整命令超时。
            self._run(["start-server"], timeout=TIMEOUT, capture=True)
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
        """检查HDC是否可用，返回 (ok, msg)
        首次检测不到设备时，自动重启HDC服务再重试一次"""
        if not os.path.isfile(self.hdc_path):
            return False, f"HDC未找到: {self.hdc_path}"
        
        ok, msg = self._check_targets()
        if ok:
            return ok, msg
        
        # 重启HDC服务后重试
        self.kill_server()
        time.sleep(1)
        self.start_server()
        time.sleep(2)
        ok, msg = self._check_targets()
        if ok:
            return ok, msg
        
        return False, "未检测到鸿蒙设备连接，请检查USB线和开发者模式（可尝试重新插拔USB）"

    def _check_targets(self):
        """内部检测设备列表"""
        try:
            r = self._run(["list", "targets"], capture=True)
            lines = r.stdout.strip().split("\n")
            devices = []
            for line in lines:
                line = line.strip()
                if line and line != "[Empty]" and not line.startswith("["):
                    devices.append(line)
            if not devices:
                return False, "无设备"
            return True, f"已连接: {devices[0]}"
        except subprocess.TimeoutExpired:
            return False, "HDC命令超时"
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

    def swipe(self, x1, y1, x2, y2, duration=500):
        """滑动操作
        鸿蒙滑动命令：hdc shell uitest uiInput swipe x1 y1 x2 y2 [velocity]
        velocity范围200-40000，根据duration换算：velocity = 距离/duration*1000
        但duration太短(<200ms)或太长(>2s)时使用固定值1500"""
        if duration and 200 <= duration <= 2000:
            distance = max(abs(x2 - x1), abs(y2 - y1), 1)
            velocity = int(min(max(distance / duration * 1000, 200), 40000))
        else:
            velocity = 1500
        self._run(["shell", "uitest", "uiInput", "swipe", str(x1), str(y1), str(x2), str(y2), str(velocity)])

    def get_screen_size(self):
        """获取屏幕分辨率（缓存结果）
        优先使用Override size（用户实际使用的分辨率），
        因为 input/uitest 命令使用的是Override坐标系而非物理分辨率"""
        if self._screen_size is not None:
            return self._screen_size
        try:
            result = self._run(["shell", "wm", "size"], capture=True)
            text = result.stdout
            # 优先取 Override size
            override_match = re.search(r'Override\s+size:\s*(\d+)x(\d+)', text)
            if override_match:
                self._screen_size = (int(override_match.group(1)), int(override_match.group(2)))
            else:
                phys_match = re.search(r'Physical\s+size:\s*(\d+)x(\d+)', text)
                if phys_match:
                    self._screen_size = (int(phys_match.group(1)), int(phys_match.group(2)))
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
