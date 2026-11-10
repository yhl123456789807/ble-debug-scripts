# 金星压测脚本 - 环境安装文档

## 前置条件

- macOS
- Python 3.9+

## 安装步骤

### 1. 创建虚拟环境（推荐）

```bash
cd ~/Desktop/金星压测脚本
python3 -m venv .venv
source .venv/bin/activate
```

### 2. 安装 venus-glasses（内部包）

```bash
pip install -e ~/Documents/code/venus-glasses
```

### 3. 安装第三方依赖

```bash
pip install -r requirements.txt
```

### 4. Android 设备初始化（仅 Android 压测需要）

```bash
python3 -m uiautomator2 init
```

### 5. iOS 设备初始化（仅 iOS 压测需要）

```bash
brew install libimobiledevice
```

## 验证安装

```bash
python3 -c "
from venus_glasses import VenusSerialTool
from uiautomator2 import Device
from tqdm import tqdm
import cv2
print('OK')
"
```

## 脚本说明

| 脚本 | 用途 |
|------|------|
| `android-bt-callback.py` | Android 蓝牙配对压测（前台） |
| `android-bt-callback-background.py` | Android 蓝牙后台回连压测 |
| `ios-bt-connect.py` | iOS 蓝牙配对压测 |

## 运行示例

```bash
# Android 前台配对压测
python android-bt-callback.py --count 100

# Android 后台回连压测
python android-bt-callback-background.py --count 100

# iOS 配对压测
python ios-bt-connect.py --count 100
```
