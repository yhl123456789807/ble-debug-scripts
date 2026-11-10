import re
import threading
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path
import time
import argparse
import logging
from typing import Optional
from venus_glasses import VenusSerialTool, ButtonEvent
from uiautomator2 import Device
from uiautomator2.exceptions import UiObjectNotFoundError
from tqdm import tqdm
from tqdm.contrib.logging import logging_redirect_tqdm


DEFAULT_PHONE_SERIAL = '10AG2A1A71006D0'
DEFAULT_GLASSES_SERIAL = '/dev/tty.usbserial-A9VQ8KI5'
DEFAULT_TEST_COUNT = 1000
DEFAULT_BLE_CONNECT_TIMEOUT = 30

_SESSION_DIR = Path(__file__).parent / 'logs' / f"{Path(__file__).stem}_{datetime.now():%Y%m%d_%H%M%S}"
FAIL_CAPTURE_DIR = _SESSION_DIR / 'failure_capture'


# 正则表达式模式
BLE_PATTERN = re.compile(r'GlassesPairingPage: BLE state changed for .* - (\w+)')
BOND_PATTERN = re.compile(r'CC-BLEPairManager.*Bond state changed for .*: (\w+) -> (\w+)')
SNAPSHOT_PATTERN = re.compile(r'Snapshot状态更新:.*connectState=ConnectState\.(\w+)')


# 系统蓝牙配对弹窗「配对」按钮
_SYSTEM_PAIR_DIALOG_XPATH = (
    '//*[@resource-id="com.android.settings:id/pairing_accept_button"]' # 小米
    ' | //*[@resource-id="android:id/button1" and (@text="配对" or @content-desc="配对")]' # 中文
    ' | //*[@resource-id="android:id/button1" and (@text="Pair" or contains(@text,"Pair") or contains(@content-desc,"Pair"))]' # 英文
    ' | //*[(@text="知道了" or @content-desc="知道了")]' # 知道了
)
# 蓝牙命令
COMMAND = {
    'enable': 'svc bluetooth enable',
    'disable': 'svc bluetooth disable',
    'status': 'getprop service.bluetooth.state',
}

# 日志监控
class LogcatMonitor:
    """实时监控 logcat 日志，提取并断言关键状态"""

    def __init__(self, adb_serial: str, bt_name: str):
        self.adb_serial = adb_serial
        self.bt_name = bt_name
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._lock = threading.Lock()
        self._log_file: Optional[IO] = None
        self._states = {
            'ble': [],      # BLE state 序列
            'bond': [],     # Bond state 序列
            'snapshot': [], # Snapshot state 序列
        }

    def _adb(self, *args: str) -> list[str]:
        """构造 adb 命令"""
        return ['adb', '-s', self.adb_serial, *args]

    def start(self) -> None:
        """启动 logcat 监控（后台线程）"""
        if self._running:
            logging.warning('LogcatMonitor 已在运行')
            return

        # 清空 logcat 缓冲区
        subprocess.run(self._adb('logcat', '-c'), capture_output=True, timeout=10)

        # 打开 logcat 日志文件
        logcat_dir = _SESSION_DIR / 'phone'
        logcat_dir.mkdir(parents=True, exist_ok=True)
        self._log_file = open(logcat_dir / 'logcat.log', 'w', encoding='utf-8')

        # 启动 logcat 进程（抓取所有日志）
        cmd = self._adb('logcat', '-v', 'time')
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding='utf-8',
                errors='replace',  # 替换无法解码的字符
                bufsize=1,  # 行缓冲
            )
            self._running = True
            self._thread = threading.Thread(target=self._read_logcat, daemon=True)
            self._thread.start()
            logging.info('LogcatMonitor 已启动')
        except Exception as ex:
            logging.error(f'LogcatMonitor 启动失败: {ex}')
            self._proc = None
            self._running = False

    def _read_logcat(self) -> None:
        """后台线程：读取 logcat 输出并解析"""
        if not self._proc or not self._proc.stdout:
            return

        try:
            for line in self._proc.stdout:
                if not self._running:
                    break

                # 写入所有日志到文件
                if self._log_file:
                    self._log_file.write(line)
                    self._log_file.flush()

                # 解析 BLE state
                ble_match = BLE_PATTERN.search(line)
                if ble_match:
                    state = ble_match.group(1)
                    with self._lock:
                        self._states['ble'].append(state)
                    logging.debug(f'BLE state: {state}')

                # 解析 Bond state
                bond_match = BOND_PATTERN.search(line)
                if bond_match:
                    from_state, to_state = bond_match.group(1), bond_match.group(2)
                    with self._lock:
                        self._states['bond'].append(to_state)
                    logging.debug(f'Bond state: {from_state} -> {to_state}')

                # 解析 Snapshot state
                snapshot_match = SNAPSHOT_PATTERN.search(line)
                if snapshot_match:
                    state = snapshot_match.group(1)
                    with self._lock:
                        self._states['snapshot'].append(state)
                    logging.debug(f'Snapshot state: {state}')

        except Exception as ex:
            logging.error(f'LogcatMonitor 读取异常: {ex}')
        finally:
            logging.info('LogcatMonitor 读取线程结束')

    def stop(self) -> None:
        """停止监控"""
        if not self._running:
            return

        self._running = False

        # 停止 logcat 进程
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

        # 等待读取线程结束
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

        self._thread = None

        # 关闭 logcat 日志文件
        if self._log_file:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None

        logging.info('LogcatMonitor 已停止')

    def wait_for_ble_state(self, expected: str, timeout: float = 10.0) -> bool:
        """等待 BLE state 达到期望状态"""
        start_time = time.time()
        while time.time() - start_time < timeout:
            with self._lock:
                if expected in self._states['ble']:
                    return True
            time.sleep(0.1)
        return False

    def wait_for_bond_state(self, expected: str, timeout: float = 10.0) -> bool:
        """等待 Bond state 达到期望状态"""
        start_time = time.time()
        while time.time() - start_time < timeout:
            with self._lock:
                if expected in self._states['bond']:
                    return True
            time.sleep(0.1)
        return False

    def wait_for_snapshot_state(self, expected: str, timeout: float = 10.0) -> bool:
        """等待 Snapshot state 达到期望状态"""
        start_time = time.time()
        while time.time() - start_time < timeout:
            with self._lock:
                if expected in self._states['snapshot']:
                    return True
            time.sleep(0.1)
        return False

    def get_state_sequence(self) -> dict:
        """获取完整的状态序列"""
        with self._lock:
            return {
                'ble': list(self._states['ble']),
                'bond': list(self._states['bond']),
                'snapshot': list(self._states['snapshot']),
            }

    def clear_states(self) -> None:
        """清空状态记录（每轮开始时调用）"""
        with self._lock:
            self._states['ble'].clear()
            self._states['bond'].clear()
            self._states['snapshot'].clear()
        logging.debug('LogcatMonitor 状态已清空')

    def __enter__(self):
        """上下文管理器入口"""
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器出口"""
        self.stop()
        return False

# 统计结果
class Statistics:
    def __init__(self) -> None:
        self.success_count = 0
        self.failure_count = 0
        self.failure_reasons: list[str] = []
        self.failure_details: list[dict] = []

    def record_success(self) -> None:
        self.success_count += 1

    def record_failure(
        self,
        round_idx: int,
        reason: str,
        screenshot_path: str | None,
        recording_path: str | None,
        state_sequence: dict | None = None,
    ) -> None:
        self.failure_count += 1
        self.failure_reasons.append(reason)
        self.failure_details.append(
            {
                'round': round_idx,
                'reason': reason,
                'screenshot': screenshot_path,
                'recording': recording_path,
                'state_sequence': state_sequence,
                'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            }
        )

    def print_stats(self, verbose: bool = False) -> None:
        total = self.success_count + self.failure_count
        rate = (self.success_count / total * 100) if total > 0 else 0.0
        logging.info('================== 统计结果 ==================')
        logging.info(f'当前已完成轮次: {total}')
        logging.info(f'成功次数: {self.success_count}')
        logging.info(f'失败次数: {self.failure_count}')
        logging.info(f'成功率: {rate:.2f}%')
        if self.failure_reasons:
            logging.info('失败原因汇总:')
            counts: dict[str, int] = {}
            for r in self.failure_reasons:
                counts[r] = counts.get(r, 0) + 1
            for reason, cnt in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
                logging.info(f'  - {reason}: {cnt} 次')
            if verbose and self.failure_details:
                logging.info('失败明细（轮次 / 时间 / 状态序列 / 截图 / 录屏）:')
                for d in self.failure_details:
                    shot = d['screenshot'] or '（无）'
                    rec = d['recording'] or '（无）'
                    seq = d.get('state_sequence')
                    seq_str = '（无）'
                    if seq:
                        ble_last3 = seq['ble'][-3:] if seq['ble'] else []
                        bond_last3 = seq['bond'][-3:] if seq['bond'] else []
                        snap_last3 = seq['snapshot'][-3:] if seq['snapshot'] else []
                        seq_str = f"BLE:{ble_last3} Bond:{bond_last3} Snap:{snap_last3}"
                    logging.info(
                        f"  - 第 {d['round']} 轮 @ {d['time']} | {d['reason']} | {seq_str} | 截图:{shot} | 录屏:{rec}"
                    )
        logging.info('================================================')

    def print_final_stats(self) -> None:
        self.print_stats(verbose=True)

# 保存失败截图
def save_failure_screenshot(phone: Device, round_idx: int) -> str | None:
    try:
        FAIL_CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        name = f'fail_r{round_idx}_{datetime.now().strftime("%Y%m%d_%H%M%S_%f")}.png'
        path = FAIL_CAPTURE_DIR / name
        phone.screenshot(str(path))
        logging.info(f'失败截图已保存: {path}')
        return str(path)
    except Exception as ex:
        logging.warning(f'保存失败截图异常: {ex}')
        return None

# 录屏
class RoundRecorder:
    """每轮 `adb shell screenrecord` 在手机上录屏；成功则 SIGINT 停录并删远端文件；失败则 adb pull 到本机。"""

    def __init__(self, adb_serial: str, round_idx: int) -> None:
        self.adb_serial = adb_serial
        self.round_idx = round_idx
        self._remote: str | None = None
        self._proc: subprocess.Popen[bytes] | None = None

    def _adb(self, *args: str) -> list[str]:
        return ['adb', '-s', self.adb_serial, *args]

    def _check_screenrecord_available(self) -> bool:
        """检查设备是否支持 screenrecord"""
        try:
            check = subprocess.run(
                self._adb('shell', 'which', 'screenrecord'),
                capture_output=True,
                timeout=5,
                text=True
            )
            if check.returncode != 0:
                logging.error('设备不支持 screenrecord 命令')
                return False
            return True
        except subprocess.TimeoutExpired:
            logging.error('检查 screenrecord 可用性超时')
            return False
        except Exception as ex:
            logging.error('检查 screenrecord 可用性失败: %s', ex)
            return False

    def start(self) -> bool:
        # 并发保护：检查是否已在录屏
        if self._proc is not None:
            logging.warning('录屏已在运行,跳过')
            return False

        # 权限检查：检查设备是否支持 screenrecord
        if not self._check_screenrecord_available():
            return False

        FAIL_CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        self._remote = f'/sdcard/venus_bt_r{self.round_idx}_{stamp}.mp4'
        cmd = self._adb('shell', 'screenrecord', '--time-limit', '300', self._remote)
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logging.info('本轮录屏开始(adb): %s', self._remote)
            return True
        except Exception as ex:
            logging.warning('录屏启动失败（跳过录屏）: %s', ex)
            self._remote = None
            self._proc = None
            return False

    def _stop(self) -> None:
        if self._proc is None:
            return
        p = self._proc
        self._proc = None

        # 发送中断信号（跨平台兼容）
        try:
            import platform
            if platform.system() == 'Windows':
                p.terminate()  # Windows 上更可靠
            else:
                p.send_signal(signal.SIGINT)  # Unix/Linux/macOS
        except ProcessLookupError:
            pass
        except Exception as ex:
            logging.warning('录屏发送中断信号: %s', ex)

        # 等待进程结束
        try:
            p.wait(timeout=25)
        except subprocess.TimeoutExpired:
            try:
                p.kill()
            except ProcessLookupError:
                pass
            try:
                p.wait(timeout=5)
            except Exception:
                pass

    def discard(self) -> None:
        self._stop()
        remote = self._remote
        self._remote = None
        if not remote:
            return
        subprocess.run(
            self._adb('shell', 'rm', '-f', remote),
            capture_output=True,
            timeout=30,
        )
        logging.info('本轮成功，已停止录屏并清理远端: %s', remote)

    def save_failure_recording(self) -> str | None:
        self._stop()
        time.sleep(1.5)  # 增加等待时间,确保视频文件完整封装
        remote = self._remote
        self._remote = None
        if not remote:
            return None
        FAIL_CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        fail_name = f'fail_r{self.round_idx}_{datetime.now().strftime("%Y%m%d_%H%M%S_%f")}.mp4'
        dest = FAIL_CAPTURE_DIR / fail_name
        pull = subprocess.run(
            self._adb('pull', remote, str(dest)),
            capture_output=True,
            timeout=120,
        )
        subprocess.run(
            self._adb('shell', 'rm', '-f', remote),
            capture_output=True,
            timeout=30,
        )
        if pull.returncode != 0:
            logging.warning(
                'adb pull 录屏失败: %s',
                (pull.stderr or b'').decode(errors='replace')[:500],
            )
            if dest.exists():
                try:
                    dest.unlink()
                except OSError:
                    pass
            return None
        if dest.exists() and dest.stat().st_size > 0:
            logging.info('失败录屏已保存: %s', dest)
            return str(dest)
        if dest.exists():
            try:
                dest.unlink()
            except OSError:
                pass
        return None

    def __enter__(self):
        """上下文管理器入口"""
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器出口"""
        if exc_type is not None:
            # 发生异常,保存失败录屏
            self.save_failure_recording()
        else:
            # 正常结束,丢弃录屏
            self.discard()

# 设置日志
def setup_logging():
    log_file = _SESSION_DIR / 'script.log'
    log_file.parent.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),
            logging.StreamHandler(sys.stderr),
        ],
        force=True,
    )
    logging.info('Logging setup complete')
 
# 初始化设备
def init_device( phone_serial: str, glasses_serial: str) -> tuple[Device, VenusSerialTool, str]:
    log_dir = _SESSION_DIR / 'glasses'
    log_dir.mkdir(parents=True, exist_ok=True)
    phone = Device(serial=phone_serial)
    phone.shell('settings put system show_touches 1')
    phone.shell('settings put system pointer_location 1')
    phone.shell('settings put system show_key_presses 1')
    glasses = VenusSerialTool(com_name=glasses_serial)
    glasses.start_log(str(log_dir))
    bt_name = ''
    while bt_name == '':
        bt_name = glasses.get_bt_name()
        logging.info(f'眼镜蓝牙名称：{bt_name}')
        time.sleep(1)
    logging.info(f'眼镜蓝牙名称：{bt_name} | 手机序列号：{phone_serial} | 眼镜串口：{glasses_serial}')
    return phone, glasses, bt_name

# 启动Venus app
def start_venus_app(phone: Device,is_cool_start: bool = False) -> bool:
    if is_cool_start:
        phone.app_start('com.rayneo.venus.test',stop=is_cool_start)
        time.sleep(8)
    else:
        phone.app_start('com.rayneo.venus.test')
    
    if phone(descriptionContains='录音').wait(timeout=5) or phone(descriptionContains='RayNeo AI').wait(timeout=5):
        logging.info('Venus app 启动成功')
    else:
        logging.error('Venus app 启动失败')
        raise Exception('Venus app 启动失败')
    return True

# 判断ble是否连接
def is_ble_connected(phone: Device,bt_name: str,timeout: int = DEFAULT_BLE_CONNECT_TIMEOUT) -> bool:
    if phone(descriptionContains='%').wait(timeout=timeout):
        logging.info('眼镜已连接')
        return True
    if phone(descriptionContains='眼镜蓝牙已断开').wait(timeout=timeout):
        logging.info('眼镜未连接')
        return False
    return True
    

# 打开系统蓝牙设置
def enter_bluetooth_settings(phone: Device) -> bool:
    result = phone.shell('am start -a android.settings.BLUETOOTH_SETTINGS')
    if result.exit_code == 0 and (
        phone(textContains='蓝牙').wait(timeout=10)
        or phone(packageName='com.oplus.wirelesssettings').wait(timeout=3)
    ):
        logging.info('已打开系统蓝牙设置页面')
    else:
        logging.error('打开系统蓝牙设置页面失败')
        raise Exception('打开系统蓝牙设置页面失败')
    return True

def get_bluetooth_on(phone: Device) -> bool:
    out = phone.shell('settings get global bluetooth_on').output.strip()
    return out == '1'


def wait_for_bluetooth(phone: Device, on: bool, timeout: float = 30.0) -> bool:
    """轮询直到系统蓝牙达到目标开/关状态。"""
    deadline = time.time() + timeout
    label = '开' if on else '关'
    while time.time() < deadline:
        if get_bluetooth_on(phone) == on:
            logging.info('蓝牙已%s', label)
            return True
        time.sleep(0.5)
    logging.warning('等待蓝牙%s超时(%ss)', label, timeout)
    return False


def _adb_set_bluetooth(phone: Device, on: bool) -> None:
    cmd = 'svc bluetooth enable' if on else 'svc bluetooth disable'
    phone.shell(cmd)
    logging.info('adb 兜底: %s', cmd)


def ensure_bluetooth(phone: Device, on: bool, timeout: float = 30.0) -> bool:
    """确保蓝牙处于目标开/关；UI 切换失败时重试并用 adb 兜底。"""
    label = '开' if on else '关'
    if get_bluetooth_on(phone) == on:
        logging.info('蓝牙已是%s，跳过点击', label)
        return True

    if on:
        time.sleep(2)

    for attempt in range(1, 4):
        if not switch_bt(phone):
            logging.warning('第 %s 次 UI 未点到蓝牙开关', attempt)
        if wait_for_bluetooth(phone, on, timeout=timeout / 3):
            return True
        logging.warning('第 %s 次切换后仍未%s，重试', attempt, label)

    _adb_set_bluetooth(phone, on)
    time.sleep(2)
    return wait_for_bluetooth(phone, on, timeout=10)

# 切换蓝牙
def switch_bt(phone: Device) -> bool:
    """
    多策略点击「蓝牙总开关」：不同 OEM / Android 版本控件 id、层级不同，按顺序尝试；
    全部失败时再尝试 adb svc（可能需用户机已允许 shell 控制蓝牙）。
    """
    # OPPO / ColorOS / OnePlus 无线设置
    BT_OPPO_SWITCH_ROW_XPATH = '//*[@text="蓝牙"]/ancestor::*[@clickable="true"][1]'
    # 三星 sesl_switchbar，避免误点「明天再开启」
    BT_MASTER_SWITCH_XPATH = (
        '//*[@resource-id="com.android.settings:id/sesl_switchbar_container"]'
    )
    BT_MASTER_SWITCH_SWITCH_XPATH = (
        '//*[@resource-id="com.android.settings:id/sesl_switchbar_switch"]'
    )
    BT_SWITCH_BAR_XPATH = '//*[@resource-id="com.android.settings:id/switch_bar"]'

    strategies = [
        (
            'text 蓝牙 同行附近 Switch',
            lambda: phone(text='蓝牙').right(className='android.widget.Switch'),
        ),
        ('xpath sesl_switchbar 行', lambda: phone.xpath(BT_MASTER_SWITCH_XPATH)),
        ('xpath sesl_switchbar Switch', lambda: phone.xpath(BT_MASTER_SWITCH_SWITCH_XPATH)),
        ('xpath switch_bar', lambda: phone.xpath(BT_SWITCH_BAR_XPATH)),
        (
            'resourceId switch_widget',
            lambda: phone(resourceId='android:id/switch_widget'),
        ),
        ('xpath OPPO 蓝牙行', lambda: phone.xpath(BT_OPPO_SWITCH_ROW_XPATH)),
        
        (
            'resourceId switch_bar',
            lambda: phone(resourceId='com.android.settings:id/switch_bar'),
        ),
        (
            'package+className Switch(设置)',
            lambda: phone(packageName='com.android.settings', className='android.widget.Switch'),
        ),
        (
            'description 含 蓝牙 的 Switch',
            lambda: phone(className='android.widget.Switch', descriptionContains='蓝牙'),
        ),

    ]

    for name, selector_fn in strategies:
        try:
            el = selector_fn()
            if el == None:
                continue
        except Exception as ex:
            logging.info(f'策略 [{name}] 构造失败: {ex}')
            continue
        try:
            if el.wait(timeout=3):
                el.click()
                logging.info(f'蓝牙已通过UI切换 ({name})')
                return True
        except Exception as ex:
            logging.info(f'策略 [{name}] 等待/点击失败: {ex}')
            continue
    logging.warning('所有 UI 策略均未命中蓝牙总开关')
    return False

# 获取眼镜蓝牙名称
def get_bt_name(glasses: VenusSerialTool) -> str:
    bt_name = ''
    count = 0
    while bt_name == '' and count < 10:
        bt_name = glasses.get_bt_name()
        logging.info(f'眼镜蓝牙名称：{bt_name}')
        time.sleep(1)
        count += 1
    return bt_name

# 蓝牙回连
def bt_callback(
    phone: Device,
    glasses: VenusSerialTool,
    bt_name: str,
    ble_connect_timeout: int = DEFAULT_BLE_CONNECT_TIMEOUT,
    monitor: LogcatMonitor | None = None,
) -> None:
    enter_bluetooth_settings(phone)

    logging.info('切换前 bluetooth_on=%s', get_bluetooth_on(phone))

    if not ensure_bluetooth(phone, on=False, timeout=25):
        raise Exception('等待蓝牙关超时')

    if not ensure_bluetooth(phone, on=True, timeout=40):
        raise Exception('等待蓝牙开超时')

    # 回连只统计「蓝牙重新打开之后」的 logcat，避免关蓝牙阶段状态干扰断言
    if monitor:
        monitor.clear_states()

    time.sleep(1)

    start_venus_app(phone, is_cool_start=False)

    if monitor:
        if not monitor.wait_for_snapshot_state('connecting', timeout=ble_connect_timeout):
            logging.warning('日志断言失败: Snapshot状态更新 未进入 connecting')
        if not monitor.wait_for_snapshot_state('connected', timeout=ble_connect_timeout):
            logging.warning('日志断言失败: Snapshot状态更新 未进入 connected')

    if (
        (monitor and monitor.wait_for_snapshot_state('connected', timeout=ble_connect_timeout))
        and is_ble_connected(phone, bt_name, timeout=ble_connect_timeout)
    ):
        logging.info('眼镜回连成功')
    else:
        raise Exception('眼镜BLE回连失败或超时')


def handle_exception(e: Exception, phone: Device, glasses: VenusSerialTool) -> None:
    logging.warning('失败恢复: %s', e)
    try:
        start_venus_app(phone, is_cool_start=True)
        time.sleep(5)
    except Exception as ex:
        logging.error('恢复流程启动 Venus 失败: %s', ex)

# 监听系统蓝牙配对弹窗
def _watcher_click_pairing_when_ready(d: Device, selector) -> None:
    """系统蓝牙配对弹窗：点击 watcher 匹配到的「配对」按钮。"""
    el = selector.get_last_match()
    if el is None:
        return
    if str(el.info.get('clickable', 'true')).lower() == 'true':
        el.click()
        rid = el.info.get('resourceName') or el.info.get('resourceId', '')
        logging.info('已点击系统配对 (%s)', rid)

def parse_args():
    parser = argparse.ArgumentParser(description='Android 蓝牙回连压测')
    parser.add_argument(
        '-p', '--phone-serial', default=DEFAULT_PHONE_SERIAL,
        help=f'手机序列号 (默认: {DEFAULT_PHONE_SERIAL})',
    )
    parser.add_argument(
        '-g', '--glasses-serial', default=DEFAULT_GLASSES_SERIAL,
        help=f'眼镜串口 (默认: {DEFAULT_GLASSES_SERIAL})',
    )
    parser.add_argument(
        '-n', '--test-count', type=int, default=DEFAULT_TEST_COUNT,
        help=f'测试次数 (默认: {DEFAULT_TEST_COUNT})',
    )
    parser.add_argument(
        '-b', '--ble-timeout', type=int, default=DEFAULT_BLE_CONNECT_TIMEOUT,
        help=f'BLE 回连超时（秒） (默认: {DEFAULT_BLE_CONNECT_TIMEOUT})',
    )
    return parser.parse_args()


def main():
    args = parse_args()
    phone_serial = args.phone_serial
    glasses_serial = args.glasses_serial
    test_count = args.test_count
    ble_timeout = args.ble_timeout

    setup_logging()
    logging.info(
        '测试参数: 手机=%s, 眼镜=%s, 次数=%s, BLE超时=%s',
        phone_serial, glasses_serial, test_count, ble_timeout,
    )

    phone, glasses, bt_name = init_device(phone_serial, glasses_serial)

    phone.watcher.when(_SYSTEM_PAIR_DIALOG_XPATH).call(_watcher_click_pairing_when_ready)
    phone.watcher.when('//*[contains(@content-desc,"立即更新") or contains(@text,"立即更新")]').press('back')
    phone.watcher.start(interval=1)

    monitor = LogcatMonitor(phone.serial, bt_name)
    monitor.start()

    stats = Statistics()

    pbar = tqdm(
        total=test_count,
        desc='蓝牙回连压测',
        unit='轮',
        dynamic_ncols=True,
        bar_format='{desc} {n}/{total} |{bar}| [{elapsed}<{remaining}, {rate_fmt}] {postfix}',
    )
    try:
        with logging_redirect_tqdm():
            for i in range(test_count):
                round_no = i + 1
                pbar.set_description(f'第{round_no}/{test_count}轮')
                logging.info('=================第%s次回连开始=================', round_no)
                monitor.clear_states()
                recorder = RoundRecorder(phone.serial, round_no)
                recorder.start()
                try:
                    bt_callback(
                        phone,
                        glasses,
                        bt_name,
                        ble_connect_timeout=ble_timeout,
                        monitor=monitor,
                    )
                except Exception as e:
                    reason = str(e)
                    shot = save_failure_screenshot(phone, round_no)
                    rec = recorder.save_failure_recording()
                    state_seq = monitor.get_state_sequence()
                    stats.record_failure(round_no, reason, shot, rec, state_seq)
                    ble_last3 = state_seq['ble'][-3:] if state_seq['ble'] else []
                    bond_last3 = state_seq['bond'][-3:] if state_seq['bond'] else []
                    snap_last3 = state_seq['snapshot'][-3:] if state_seq['snapshot'] else []
                    logging.error(
                        '第%s次回连失败：%s%s%s | BLE:%s Bond:%s Snap:%s',
                        round_no,
                        reason,
                        f' | 截图:{shot}' if shot else ' | 截图:无',
                        f' | 录屏:{rec}' if rec else ' | 录屏:无',
                        ble_last3, bond_last3, snap_last3,
                    )
                    stats.print_stats(verbose=False)
                    handle_exception(e, phone, glasses)
                else:
                    stats.record_success()
                    recorder.discard()
                    state_seq = monitor.get_state_sequence()
                    total = stats.success_count + stats.failure_count
                    rate = stats.success_count / total * 100 if total > 0 else 0.0
                    ble_last3 = state_seq['ble'][-3:] if state_seq['ble'] else []
                    bond_last3 = state_seq['bond'][-3:] if state_seq['bond'] else []
                    snap_last3 = state_seq['snapshot'][-3:] if state_seq['snapshot'] else []
                    logging.info(
                        '=================第%s次回连成功================= | '
                        '成功:%s 失败:%s 成功率:%.2f%% | '
                        'BLE:%s Bond:%s Snap:%s',
                        round_no, stats.success_count, stats.failure_count, rate,
                        ble_last3, bond_last3, snap_last3,
                    )
                    stats.print_stats(verbose=False)
                done = stats.success_count + stats.failure_count
                rate = stats.success_count / done * 100 if done > 0 else 0.0
                pbar.set_postfix(成功=stats.success_count, 失败=stats.failure_count, 成功率=f'{rate:.1f}%')
                pbar.update(1)
    except KeyboardInterrupt:
        logging.info('收到键盘中断，结束压测')
    finally:
        pbar.close()
        monitor.stop()
        stats.print_final_stats()


if __name__ == '__main__':
    main()
