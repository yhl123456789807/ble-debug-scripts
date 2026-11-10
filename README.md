# ble-debug-scripts
Android BLE、ESP32 蓝牙自动化调试 & 日志解析脚本集合

## 简介
用于蓝牙设备自动化测试、日志抓取、日志解析、功耗统计的Python脚本，适配AR眼镜、蓝牙耳机、BLE外设调试。

## 目录
- `venus_bt_scrpit`：Venus蓝牙相关自动化脚本
- 后续新增：功耗计算、bugreport日志解析、BLE报文抓包脚本

## 环境依赖
```bash
pip install -r requirements.txt

# 安卓自动化环境部署
1、执行运行脚本即可

# iOS自动化环境部署
## 1. 安装 go-ios
> 其概念与adb类似，由于是单exe文件，可以直接放在跟adb同级目录下，即可直接使用
- 版本：v1.0.206

1. 将 `go-ios-win.zip` 进行解压，把 `ios.exe` 放到某个目录下，并将目录配置到环境变量
2. 将 `wintun-0.14.1.zip` 进行解压，拷贝 `wintun\bin\amd64` 目录下的 `wintun.dll` 到 `C:\Windows\System32` 目录下

## 2. 安装 iTunes
安装包：`iTunes Installer (1).exe`

## 3. 构建 WDA
1. 需要 macOS 并安装 Xcode 才能进行构建
2. 需要苹果开发者账号（免费vs订阅版本有区别）
- Xcode 方式见配套文档
- 深圳互联实验室配置 WebDriverAgent(WDA) 步骤

## 4. 安装 WDA
1. 鼠标右键点击屏幕中间底部的Win图标，选择【终端管理员】或以管理员运行终端
```bash
# 1. 以终端管理员打开命令行，启动隧道
ios tunnel start --userspace

# 2. 查看设备UDID
ios list

# 3. 安装 WDA 到手机上
ios install --udid=你的设备UDID --path=/path/to/WDA.ipa

# 以下命令已经在工具托管
# 点击启动BOX 按钮即可
# 8100 端口给UI自动化测试使用
ios forward 8100 8100
# 9100 端口被屏幕预览使用
ios forward 9100 9100
# 启动 WDA 后能在屏幕上看到一行水印 "Automation Running"
ios runwda --bundleid com.yourcompany.WebDriverAgentRunner.xctrunner --testrunnerbundleid com.yourcompany.WebDriverAgentRunner.xctrunner --xctestconfig WebDriverAgentRunner.xctest
