---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: '358dc0d2-9429-4ce5-89d8-aad8b9e01f0e'
  PropagateID: '358dc0d2-9429-4ce5-89d8-aad8b9e01f0e'
  ReservedCode1: 'd2a65bce-727a-48f4-96da-0c92b149e22d'
  ReservedCode2: 'd2a65bce-727a-48f4-96da-0c92b149e22d'
---

# BOSS直聘自动投递工具

基于 ADB + OCR 的 BOSS直聘自动投递桌面工具，通过安卓手机自动化操作实现批量投递简历。

## 功能特性

- **关键词过滤**：支持包含/排除关键词，中英文逗号、空格分隔
- **薪资范围筛选**：可设定最低/最高薪资（K），自动跳过不符岗位
- **自动投递**：点击"立即沟通"自动投递，检测到"继续沟通"则跳过
- **防风控横滑**：智能横滑切换岗位，避开安卓边缘手势
- **日志记录**：投递日志异步写入，`│` 分隔 + `✓/✗/↻` 图标美化
- **配置持久化**：配置文件保存到 exe 同级目录，重启不丢失

## 技术栈

- **GUI**：tkinter（原生 Python GUI）
- **OCR**：RapidOCR（基于 ONNX Runtime，PP-OCRv4 模型）
- **设备控制**：ADB（Android Debug Bridge）
- **打包**：PyInstaller 单文件 exe，源码 zlib+base64 加密

## 文件说明

| 文件 | 说明 |
|------|------|
| `boss_gui.py` | 主程序（GUI + 投递逻辑） |
| `boss_ocr.py` | OCR 识别模块（ADB截图 → 裁剪 → RapidOCR识别） |
| `boss.ico` | 应用图标 |
| `安装依赖.bat` | 首次运行安装所需 Python 依赖 |
| `启动BOSS投递.bat` | 一键启动工具 |

## 使用方法

### 源码运行

1. 安装 Python 3.12+，确保 ADB 已配置
2. 运行 `安装依赖.bat` 或手动安装：
   ```bash
   pip install rapidocr_onnxruntime[onnxruntime] opencv-python pillow pyclipper shapely
   ```
3. 安卓手机开启 USB 调试，连接电脑
4. 运行 `启动BOSS投递.bat` 或 `python boss_gui.py`

### exe 运行

直接双击 exe 即可，所有依赖（ADB、OCR模型）已内置，无需额外安装。

## 打包说明

使用 PyInstaller 打包为单文件 exe，支持源码加密：

```bash
# 1. 运行加密脚本
python .temp/encrypt.py

# 2. 修改 boss.spec 入口为 boss_gui_enc.py

# 3. 打包
PyInstaller boss.spec --noconfirm
```

## 注意事项

- 需要安卓手机通过 USB 连接并开启调试模式
- 手机屏幕分辨率需与工具适配（默认 1080x2400 级别）
- 投递间隔建议不低于 3 秒，避免触发平台风控

> AI生成