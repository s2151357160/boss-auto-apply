# device_driver.py
# 功能：设备驱动抽象层，支持安卓(ADB)和鸿蒙(HDC)

import os
import sys
import re
import subprocess
import time
import random
import shutil

# ============ 常量 ============
SUBPROC_FLAGS = 0x08000000  # CREATE_NO_WINDOW，隐藏命令行窗口
TIMEOUT = 15
SHORT_TIMEOUT = 5

# ============ 模拟器端口映射 ============
# 名称 -> ADB连接地址，端口为0表示真机（USB连接，无需connect）
EMULATOR_OPTIONS = [
    ("none",   "真机 (USB)",       0),
    ("mumu12", "MuMu模拟器12",     16384),
    ("mumu6",  "MuMu模拟器6",      7555),
    ("ldx",    "雷电模拟器",       5555),
    ("yeshen", "夜神模拟器",       62001),
    ("xiaoyao", "逍遥模拟器",      21503),
]


def get_emulator_port(emulator_id):
    """根据模拟器ID返回ADB端口，真机返回0"""
    for eid, _, port in EMULATOR_OPTIONS:
        if eid == emulator_id:
            return port
    return 0

# ============ 驱动基类 ============
class DeviceDriver:
    """设备驱动基类，定义统一接口"""

    name = "base"

    def check_connection(self):
        """检查设备连接，返回 (ok, msg)"""
        raise NotImplementedError

    def screenshot(self, save_path, max_retry=3):
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

    def __init__(self, adb_path, emulator_port=0):
        self.adb_path = adb_path
        self.emulator_port = emulator_port  # 0=真机USB，>0=模拟器网络ADB端口
        self._serial = None  # 目标设备序列号，多设备时用于-s参数
        self._screen_size = None

    def _run(self, args, timeout=SHORT_TIMEOUT, check=False, capture=False, target=True):
        """执行ADB命令的统一封装
        target=True时自动添加-s序列号（shell/pull等设备级命令）
        target=False时不添加（devices/connect/kill-server等服务级命令）"""
        cmd = [self.adb_path]
        if target and self._serial:
            cmd += ['-s', self._serial]
        cmd += args
        kwargs = dict(timeout=timeout, creationflags=SUBPROC_FLAGS)
        if capture:
            kwargs["capture_output"] = True
            kwargs["text"] = True
        if check:
            kwargs["check"] = True
        return subprocess.run(cmd, **kwargs)

    def check_connection(self):
        """检查ADB是否可用，返回 (ok, msg)
        模拟器模式：先adb connect再检测
        真机模式：首次检测不到设备时，自动重启ADB服务再重试一次"""
        if not os.path.isfile(self.adb_path):
            return False, f"ADB未找到: {self.adb_path}"
        
        # 模拟器模式：先connect再检测
        if self.emulator_port > 0:
            addr = f"127.0.0.1:{self.emulator_port}"
            conn_ok, conn_msg = self._connect_emulator(addr)
            ok, msg = self._check_devices()
            if ok:
                return ok, msg
            # 重试一次
            if not conn_ok:
                self._connect_emulator(addr)
            time.sleep(1)
            ok, msg = self._check_devices()
            if ok:
                return ok, msg
            return False, f"模拟器未连接: {addr}，请确认模拟器已启动且端口正确"
        
        # 真机模式：原有逻辑
        # 尝试检测设备
        ok, msg = self._check_devices()
        if ok:
            return ok, msg
        
        # 首次没找到，重启ADB服务后重试
        self.kill_server()
        time.sleep(1)
        self.start_server()
        time.sleep(2)
        ok, msg = self._check_devices()
        if ok:
            return ok, msg
        
        # 仍然没有，提示用户
        return False, "未检测到手机连接，请检查USB线和USB调试（可尝试重新插拔USB）"

    def _connect_emulator(self, addr):
        """连接模拟器（网络ADB），返回 (ok, msg)"""
        try:
            r = self._run(["connect", addr], timeout=5, capture=True, target=False)
            out = r.stdout.strip()
            # adb connect 成功输出含 "connected"，已连接含 "already connected"
            if "connected" in out:
                return True, out
            # "failed to connect" / "cannot connect" 等视为失败
            return False, out
        except Exception as e:
            return False, str(e)

    def _check_devices(self):
        """内部检测设备列表，同时设置self._serial（多设备时选目标设备）"""
        try:
            r = self._run(["devices"], capture=True, target=False)
            devices = []
            for line in r.stdout.strip().split("\n")[1:]:
                line = line.strip()
                if not line:
                    continue
                # 按列匹配：设备行格式为 "序列号\t状态"，状态列必须为 "device"
                parts = line.replace("\t", " ").split()
                if len(parts) >= 2 and parts[1] == "device":
                    devices.append(parts[0])
            if not devices:
                self._serial = None
                return False, "无设备"
            
            # 模拟器模式：优先匹配端口地址
            if self.emulator_port > 0:
                addr = f"127.0.0.1:{self.emulator_port}"
                if addr in devices:
                    self._serial = addr
                else:
                    # 地址不匹配时报错而非盲目取第一个（避免误操作其他模拟器）
                    self._serial = None
                    return False, f"模拟器未连接: {addr}，当前设备列表: {devices}，请确认模拟器已启动且端口正确"
            else:
                # 真机模式：优先USB设备（排除emulator-*和IP:端口格式的模拟器/网络设备）
                usb_devices = [d for d in devices if not d.startswith("emulator-") and ":" not in d]
                if usb_devices:
                    self._serial = usb_devices[0]
                else:
                    self._serial = devices[0]  # 回退：取第一个
            
            return True, f"已连接: {self._serial}"
        except subprocess.TimeoutExpired:
            return False, "ADB命令超时"
        except Exception as e:
            return False, f"ADB检查失败: {e}"

    def screenshot(self, save_path, max_retry=3):
        """ADB截图，失败自动重试
        远程路径加PID后缀避免多实例共享冲突；pull到英文临时路径再复制到中文路径"""
        remote_path = f"/sdcard/screenshot_{os.getpid()}.png"
        pull_tmp = os.path.join(os.environ.get("TEMP", "/tmp"), f"boss_pull_{os.getpid()}.png")
        for i in range(max_retry):
            try:
                self._run(["shell", "screencap", "-p", remote_path], timeout=TIMEOUT, check=True)
                self._run(["pull", remote_path, pull_tmp], timeout=TIMEOUT, check=True)
                self._run(["shell", "rm", remote_path])
                # 复制到最终路径（处理中文/空格路径）
                shutil.copy2(pull_tmp, save_path)
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
        """随机偏移点击，check=True确保失败时抛异常"""
        rx = max(0, x + random.randint(-offset, offset))
        ry = max(0, y + random.randint(-offset, offset))
        self._run(["shell", "input", "tap", str(rx), str(ry)], check=True)

    def swipe(self, x1, y1, x2, y2, duration=500):
        """滑动操作，duration最低500ms兼容更多ROM，check=True确保失败时抛异常"""
        duration = max(duration, 500)
        self._run(["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration)], check=True)

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
            self._run(["kill-server"], target=False)
        except Exception:
            pass

    def start_server(self):
        """启动ADB服务"""
        try:
            self._run(["start-server"], target=False)
        except Exception:
            pass


# ============ 鸿蒙 HDC 驱动 ============
class HDCDriver(DeviceDriver):
    """鸿蒙设备驱动，基于 HDC 命令"""

    name = "harmony"

    def __init__(self, hdc_path):
        self.hdc_path = hdc_path
        self._serial = None  # 目标设备序列号，多设备时用于-t参数
        self._screen_size = None

    def _run(self, args, timeout=SHORT_TIMEOUT, check=False, capture=False, target=True):
        """执行HDC命令的统一封装
        target=True时自动添加-t序列号（shell/file等设备级命令）
        target=False时不添加（list/kill等服务级命令）"""
        cmd = [self.hdc_path]
        if target and self._serial:
            cmd += ['-t', self._serial]
        cmd += args
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
        """内部检测设备列表，同时设置self._serial"""
        try:
            r = self._run(["list", "targets"], capture=True, target=False)
            lines = r.stdout.strip().split("\n")
            devices = []
            for line in lines:
                line = line.strip()
                if line and line != "[Empty]" and not line.startswith("["):
                    devices.append(line)
            if not devices:
                self._serial = None
                return False, "无设备"
            self._serial = devices[0]
            return True, f"已连接: {self._serial}"
        except subprocess.TimeoutExpired:
            return False, "HDC命令超时"
        except Exception as e:
            return False, f"HDC检查失败: {e}"

    def screenshot(self, save_path, max_retry=3):
        """HDC截图，失败自动重试
        鸿蒙截图命令：hdc shell snapshot_display -f /data/local/tmp/screenshot.jpeg
        注意：鸿蒙 snapshot_display 只支持 .jpeg 后缀，不能用 .png
        拉取命令：hdc file recv /data/local/tmp/screenshot.jpeg local_path
        远程路径加PID后缀避免多实例共享冲突"""
        remote_path = f"/data/local/tmp/screenshot_{os.getpid()}.jpeg"
        for i in range(max_retry):
            try:
                self._run(["shell", "snapshot_display", "-f", remote_path],
                          timeout=TIMEOUT, check=True)
                # 先pull到英文临时路径（避免中文/空格路径导致pull失败），再复制到最终路径
                pull_tmp = os.path.join(os.environ.get("TEMP", "/tmp"), f"boss_hdc_pull_{os.getpid()}.jpeg")
                self._run(["file", "recv", remote_path, pull_tmp],
                          timeout=TIMEOUT, check=True)
                # 清理手机端截图
                try:
                    self._run(["shell", "rm", remote_path])
                except Exception:
                    pass
                # 复制到最终路径（处理中文/空格路径）
                shutil.copy2(pull_tmp, save_path)
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
        """随机偏移点击，check=True确保失败时抛异常
        鸿蒙点击命令：hdc shell uitest uiInput click x y"""
        rx = max(0, x + random.randint(-offset, offset))
        ry = max(0, y + random.randint(-offset, offset))
        self._run(["shell", "uitest", "uiInput", "click", str(rx), str(ry)], check=True)

    def swipe(self, x1, y1, x2, y2, duration=500):
        """滑动操作，check=True确保失败时抛异常
        鸿蒙滑动命令：hdc shell uitest uiInput swipe x1 y1 x2 y2 [velocity]
        velocity范围200-40000，根据duration换算：velocity = 距离/duration*1000
        但duration太短(<200ms)或太长(>2s)时使用固定值1500"""
        if duration and 200 <= duration <= 2000:
            distance = max(abs(x2 - x1), abs(y2 - y1), 1)
            velocity = int(min(max(distance / duration * 1000, 200), 40000))
        else:
            velocity = 1500
        self._run(["shell", "uitest", "uiInput", "swipe", str(x1), str(y1), str(x2), str(y2), str(velocity)], check=True)

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
            self._run(["kill"], target=False)
        except Exception:
            pass

    def start_server(self):
        """启动HDC服务（HDC通常自动启动，无需手动）"""
        pass


# ============ 驱动工厂 ============
def create_driver(platform, tool_dir=None, emulator_id="none"):
    """根据平台创建驱动实例
    platform: 'android' 或 'harmony'
    tool_dir: 平台工具所在目录，None则自动查找
    emulator_id: 模拟器ID，'none'=真机USB，其他为模拟器网络ADB"""
    if platform == "android":
        adb_path = _find_tool("adb.exe", "platform-tools", tool_dir)
        emu_port = get_emulator_port(emulator_id)
        return ADBDriver(adb_path, emulator_port=emu_port)
    elif platform == "harmony":
        hdc_path = _find_tool("hdc.exe", "hdc", tool_dir)
        return HDCDriver(hdc_path)
    else:
        raise ValueError(f"不支持的平台: {platform}")


def _find_tool(exe_name, sub_dir, tool_dir=None):
    """查找工具路径，优先级：_MEIPASS > Nuitka临时目录 > tool_dir > WORK_DIR > 脚本目录"""
    # 1. PyInstaller 打包模式：从 _MEIPASS 临时目录查找
    _meipass = getattr(sys, '_MEIPASS', '')
    if _meipass:
        path = os.path.join(_meipass, sub_dir, exe_name)
        if os.path.isfile(path):
            return path

    # 1b. Nuitka打包模式：从临时解压目录查找（sys.executable指向onefile临时目录）
    if not _meipass and not getattr(sys, 'frozen', False):
        # Nuitka: frozen=False但__compiled__存在
        _main = sys.modules.get('__main__')
        if _main and hasattr(_main, '__compiled__'):
            _nuitka_tmp = os.path.dirname(sys.executable)
            path = os.path.join(_nuitka_tmp, sub_dir, exe_name)
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
        # PyInstaller: sys.executable指向exe本身
        work_dir = os.path.dirname(sys.executable)
    else:
        # Nuitka: sys.argv[0]指向原始exe路径；脚本模式: __file__指向源码
        _main = sys.modules.get('__main__')
        if _main and hasattr(_main, '__compiled__'):
            work_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
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
