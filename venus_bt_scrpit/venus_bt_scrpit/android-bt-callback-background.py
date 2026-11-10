"""
Android 蓝牙后台回连压测

场景：连接态退出APP（APP在后台）-> 从系统设置关闭蓝牙 -> 等待15s -> 开启蓝牙 -> 等待10s -> 检查后台回连成功（不打开APP）
"""
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
from uiautomator2.exceptions import UiObjectNotFoundError,ConnectError
from tqdm import tqdm
from tqdm.contrib.logging import logging_redirect_tqdm


DEFAULT_PHONE_SERIAL = 'F6VCI7DMBUS4IBSC'
DEFAULT_GLASSES_SERIAL = '/dev/tty.usbserial-A9VQ8KI5'
DEFAULT_TEST_COUNT = 1000
DEFAULT_BT_OFF_WAIT = 15
DEFAULT_BT_ON_WAIT = 10
DEFAULT_RECONNECT_TIMEOUT = 30
DEFAULT_BT_SCAN_TIMEOUT = 35
DEFAULT_BT_CONNECT_TIMEOUT = 30

_SESSION_DIR = Path(__file__).parent / 'logs' / f"{Path(__file__).stem}_{datetime.now():%Y%m%d_%H%M%S}"
FAIL_CAPTURE_DIR = _SESSION_DIR / 'failure_capture'


# 正则表达式模式
BLE_PATTERN = re.compile(r'GlassesPairingPage: BLE state changed for .* - (\w+)')
BOND_PATTERN = re.compile(r'CC-BLEPairManager.*Bond state changed for .*: (\w+) -> (\w+)')
SNAPSHOT_PATTERN = re.compile(r'Snapshot状态更新:.*connectState=ConnectState\.(\w+)')
GLASSES_CONN_STATE_PATTERN = re.compile(r'dump conn state:.*rnlink:(\d+)')


# 眼镜端连接状态监控（基于 VenusSerialTool register_watcher）
class GlassesConnMonitor:
    """通过 VenusSerialTool 的 watcher 机制实时监控眼镜端 rnlink 连接状态"""

    WATCH_KEYWORD = 'dump conn state'

    def __init__(self, glasses: VenusSerialTool) -> None:
        self._glasses = glasses
        self._lock = threading.Lock()
        self._states: list[tuple[str, float]] = []  # (rnlink_value, timestamp)
        self._registered = False

    def _on_conn_state(self, line: str) -> None:
        match = GLASSES_CONN_STATE_PATTERN.search(line)
        if match:
            value = match.group(1)
            with self._lock:
                self._states.append((value, time.time()))
            logging.debug('眼镜端 rnlink=%s', value)

    def start(self) -> None:
        if self._registered:
            return
        self._glasses.register_watcher(self.WATCH_KEYWORD, self._on_conn_state)
        self._registered = True
        logging.info('GlassesConnMonitor 已启动')

    def stop(self) -> None:
        if not self._registered:
            return
        self._glasses.unregister_watcher(self.WATCH_KEYWORD)
        self._registered = False
        logging.info('GlassesConnMonitor 已停止')

    def clear_states(self) -> None:
        with self._lock:
            self._states.clear()

    def wait_for_rnlink(self, expected: str, timeout: float = 30.0) -> bool:
        """等待眼镜端 rnlink 达到期望值（'1'=连接, '0'=断开）"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                for value, _ in reversed(self._states):
                    if value == expected:
                        return True
            time.sleep(0.5)
        return False

    def wait_for_reconnect(self, timeout: float = 30.0) -> bool:
        """等待眼镜端完成断连→重连（rnlink 先变为 0，再变为 1）

        在状态序列中找到第一个 '0' 的位置，然后检查其后是否出现 '1'。
        避免误判初始状态中的旧 '1'。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                states = list(self._states)
            # 找到第一个 0 的位置
            idx_0 = -1
            for i, (value, _) in enumerate(states):
                if value == '0':
                    idx_0 = i
                    break
            # 在 0 之后找第一个 1
            if idx_0 >= 0:
                for value, _ in states[idx_0 + 1:]:
                    if value == '1':
                        return True
            time.sleep(0.5)
        logging.warning('眼镜端断言: rnlink 未完成 0→1 转换 (序列:%s)',
                        [v for v, _ in self._states[:]])
        return False

    def get_state_sequence(self) -> list[str]:
        with self._lock:
            return [v for v, _ in self._states]

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
        return False


# 系统蓝牙配对弹窗「配对」按钮
_SYSTEM_PAIR_DIALOG_XPATH = (
    '//*[@resource-id="com.android.settings:id/pairing_accept_button"]'#小米
    ' | //*[@resource-id="com.android.companiondevicemanager:id/btn_positive"]' #vivo
    ' | //*[@resource-id="android:id/button1" and (@text="配对" or @content-desc="配对")]' # 中文
    ' | //*[@resource-id="android:id/button1" and (@text="允许" or @content-desc="允许")]' # 中文
    ' | //*[@resource-id="android:id/button1" and (@text="Pair" or contains(@text,"Pair")' # 英文
    ' or contains(@content-desc,"Pair"))]'
)


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
            'ble': [],
            'bond': [],
            'snapshot': [],
        }

    def _adb(self, *args: str) -> list[str]:
        return ['adb', '-s', self.adb_serial, *args]

    def start(self) -> None:
        if self._running:
            logging.warning('LogcatMonitor 已在运行')
            return

        subprocess.run(self._adb('logcat', '-c'), capture_output=True, timeout=10)

        logcat_dir = _SESSION_DIR / 'phone'
        logcat_dir.mkdir(parents=True, exist_ok=True)
        self._log_file = open(logcat_dir / 'logcat.log', 'w', encoding='utf-8')

        cmd = self._adb('logcat', '-v', 'time')
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding='utf-8',
                errors='replace',
                bufsize=1,
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
        if not self._proc or not self._proc.stdout:
            return

        try:
            for line in self._proc.stdout:
                if not self._running:
                    break

                if self._log_file:
                    self._log_file.write(line)
                    self._log_file.flush()

                ble_match = BLE_PATTERN.search(line)
                if ble_match:
                    state = ble_match.group(1)
                    with self._lock:
                        self._states['ble'].append(state)
                    logging.debug(f'BLE state: {state}')

                bond_match = BOND_PATTERN.search(line)
                if bond_match:
                    from_state, to_state = bond_match.group(1), bond_match.group(2)
                    with self._lock:
                        self._states['bond'].append(to_state)
                    logging.debug(f'Bond state: {from_state} -> {to_state}')

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
        if not self._running:
            return

        self._running = False

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

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

        self._thread = None

        if self._log_file:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None

        logging.info('LogcatMonitor 已停止')

    def wait_for_ble_state(self, expected: str, timeout: float = 10.0) -> bool:
        start_time = time.time()
        while time.time() - start_time < timeout:
            with self._lock:
                if expected in self._states['ble']:
                    return True
            time.sleep(0.1)
        return False

    def wait_for_bond_state(self, expected: str, timeout: float = 10.0) -> bool:
        start_time = time.time()
        while time.time() - start_time < timeout:
            with self._lock:
                if expected in self._states['bond']:
                    return True
            time.sleep(0.1)
        return False

    def wait_for_snapshot_state(self, expected: str, timeout: float = 10.0) -> bool:
        start_time = time.time()
        while time.time() - start_time < timeout:
            with self._lock:
                if expected in self._states['snapshot']:
                    return True
            time.sleep(0.1)
        return False

    def get_state_sequence(self) -> dict:
        with self._lock:
            return {
                'ble': list(self._states['ble']),
                'bond': list(self._states['bond']),
                'snapshot': list(self._states['snapshot']),
            }

    def clear_states(self) -> None:
        with self._lock:
            self._states['ble'].clear()
            self._states['bond'].clear()
            self._states['snapshot'].clear()
        logging.debug('LogcatMonitor 状态已清空')

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
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
        try:
            check = subprocess.run(
                self._adb('shell', 'which', 'screenrecord'),
                capture_output=True,
                timeout=5,
                text=True,
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
        if self._proc is not None:
            logging.warning('录屏已在运行,跳过')
            return False

        if not self._check_screenrecord_available():
            return False

        FAIL_CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        self._remote = f'/sdcard/venus_bt_bg_r{self.round_idx}_{stamp}.mp4'
        cmd = self._adb('shell', 'screenrecord', '--time-limit', '300', self._remote)
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logging.info('本轮录屏开始: %s', self._remote)
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

        try:
            import platform
            if platform.system() == 'Windows':
                p.terminate()
            else:
                p.send_signal(signal.SIGINT)
        except ProcessLookupError:
            pass
        except Exception as ex:
            logging.warning('录屏发送中断信号: %s', ex)

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
        time.sleep(1.5)
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
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.save_failure_recording()
        else:
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
def init_device(phone_serial: str, glasses_serial: str) -> tuple[Device, VenusSerialTool, str]:
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
def start_venus_app(phone: Device, is_cool_start: bool = False) -> bool:
    if is_cool_start:
        phone.app_start('com.rayneo.venus.test', stop=is_cool_start)
        time.sleep(8)
    else:
        phone.app_start('com.rayneo.venus.test')

    if phone(descriptionContains='配对眼镜').wait(timeout=5) or phone(descriptionContains='RayNeo AI').wait(timeout=5):
        logging.info('Venus app 启动成功')
    else:
        logging.error('Venus app 启动失败')
        raise Exception('Venus app 启动失败')
    return True


# 切换到眼镜tab
def switch_to_glasses_tab(phone: Device) -> bool:
    phone(description='眼镜', className='android.widget.Button', clickable=True).click(timeout=5)
    if phone(descriptionContains='配对眼镜').wait(timeout=3) or phone(descriptionContains='录音').wait(timeout=3):
        logging.info('切换到眼镜tab成功')
    else:
        logging.error('切换到眼镜tab失败')
        raise Exception('切换到眼镜tab失败')
    return True


# 判断眼镜是否连接
def is_connected(phone: Device, bt_name: str) -> bool:
    if phone(descriptionContains='配对眼镜').wait(timeout=2):
        logging.info('眼镜未连接')
        return False
    elif phone(descriptionContains=bt_name).wait(timeout=2):
        logging.info('眼镜已连接')
        return True
    return True


# 进入连接页面
def enter_connect_page(phone: Device) -> bool:
    if phone(description='配对眼镜').wait(timeout=5):
        phone(description='配对眼镜', clickable=True).click(timeout=5)
    if (
        phone(descriptionContains='正在搜索眼镜').wait(timeout=8)
        or phone(descriptionContains='Found the following glasses').wait(timeout=5)
        or phone(descriptionContains='请选择你的眼镜').wait(timeout=5)
    ):
        logging.info('进入连接页面成功')
    else:
        logging.error('进入连接页面失败')
        raise Exception('进入连接页面失败')
    return True


# 连接眼镜
def connect_glasses(phone: Device, bt_name: str, bt_connect_timeout: int, bt_scan_timeout: int, monitor: LogcatMonitor | None = None) -> bool:
    pkg = 'com.rayneo.venus.test'
    scroll = phone(packageName=pkg, className='android.widget.ScrollView', scrollable=True)
    has_scroll = scroll.wait(timeout=5)
    if not has_scroll:
        scroll = phone(packageName=pkg, scrollable=True)
        has_scroll = scroll.wait(timeout=3)
    if not has_scroll:
        scroll = None
        logging.info('当前页面无可滚动容器，改用手势滑动定位设备')

    def _footer_top() -> int:
        top = phone.window_size()[1]
        for desc in ('找不到你的眼镜', '重新搜索'):
            footer = phone(packageName=pkg, descriptionContains=desc)
            if footer.exists:
                top = min(top, footer.info['bounds']['top'])
        return top

    def _target_pairing():
        row = phone(packageName=pkg, description=bt_name)
        if not row.exists:
            row = phone(packageName=pkg, descriptionContains=bt_name)
        if not row.exists:
            return None
        btn = row.child(description='配对')
        if btn.exists:
            return btn
        xpath_btn = phone.xpath(
            f'//*[contains(@content-desc,"{bt_name}")]//*[@content-desc="配对"]'
        )
        return xpath_btn if xpath_btn.exists else None

    def _pairing_clickable(btn) -> bool:
        if btn is None or not btn.exists:
            return False
        b = btn.info['bounds']
        return b['bottom'] < _footer_top() - 12

    def _scroll_list_down():
        if has_scroll and scroll is not None:
            try:
                scroll.scroll.vert.forward(steps=12)
            except UiObjectNotFoundError:
                pass
        phone.swipe(0.5, 0.72, 0.5, 0.38, steps=20)
        time.sleep(0.35)

    def _scroll_list_up():
        if has_scroll and scroll is not None:
            try:
                scroll.scroll.vert.backward(steps=12)
            except UiObjectNotFoundError:
                pass
        phone.swipe(0.5, 0.38, 0.5, 0.72, steps=20)
        time.sleep(0.35)

    def _scroll_to_target():
        if has_scroll and scroll is not None:
            try:
                scroll.scroll.to(descriptionContains=bt_name)
            except UiObjectNotFoundError:
                pass

    def _reveal_pairing(btn, max_steps: int = 10) -> bool:
        for _ in range(max_steps):
            if _pairing_clickable(btn):
                return True
            _scroll_list_down()
        return _pairing_clickable(btn)

    pairing = None
    deadline = time.monotonic() + bt_scan_timeout
    scan_phase = 0
    down_steps = 0

    if has_scroll and scroll is not None:
        try:
            scroll.scroll.toBeginning()
        except UiObjectNotFoundError:
            pass
        time.sleep(0.3)

    while time.monotonic() < deadline:
        pairing = _target_pairing()
        if pairing is not None:
            logging.info('已定位目标设备: %s', bt_name)
            break

        _scroll_to_target()
        pairing = _target_pairing()
        if pairing is not None:
            logging.info('已定位目标设备: %s (scroll.to)', bt_name)
            break

        if scan_phase == 0:
            _scroll_list_down()
            down_steps += 1
            if down_steps >= 12:
                if has_scroll and scroll is not None:
                    try:
                        scroll.scroll.toEnd()
                    except UiObjectNotFoundError:
                        pass
                    time.sleep(0.4)
                scan_phase = 1
        else:
            _scroll_list_up()
        time.sleep(0.35)
    else:
        logging.error('连接眼镜失败：列表中未找到目标设备')
        raise Exception('连接眼镜失败:列表中未找到目标设备')

    if not pairing.wait(timeout=5):
        raise Exception('连接眼镜失败：未找到 配对 按钮')

    if not _reveal_pairing(pairing):
        raise Exception('连接眼镜失败：配对按钮被页脚遮挡')

    pairing.click(timeout=5)
    logging.info('点击配对成功')

    if monitor:
        if not monitor.wait_for_ble_state('connecting', timeout=10):
            logging.warning('日志断言失败: BLE 未进入 connecting')
        if not monitor.wait_for_ble_state('connected', timeout=10):
            logging.warning('日志断言失败: BLE 未进入 connected')
        if not monitor.wait_for_ble_state('clientReady', timeout=10):
            logging.warning('日志断言失败: BLE 未进入 clientReady')
        if not monitor.wait_for_ble_state('authSuccess', timeout=10):
            logging.warning('日志断言失败: BLE 未进入 authSuccess')

        if not monitor.wait_for_bond_state('BOND_BONDING', timeout=10):
            logging.warning('日志断言失败: 未进入 BOND_BONDING')
        if not monitor.wait_for_bond_state('BOND_BONDED', timeout=15):
            logging.warning('日志断言失败: 未进入 BOND_BONDED')

        if not monitor.wait_for_snapshot_state('connected', timeout=10):
            logging.warning('日志断言失败: Snapshot 未进入 connected')

    if (
        (monitor and monitor.wait_for_snapshot_state('connected', timeout=10))
        or phone(descriptionContains='配对成功').wait(timeout=bt_connect_timeout)
    ):
        time.sleep(1)
        logging.info('眼镜配对成功')
        return True
    else:
        logging.error('眼镜配对失败')
        raise Exception('眼镜配对失败')


_VENUS_PKG = 'com.rayneo.venus.test'


def _is_stale_error(ex: BaseException) -> bool:
    return 'StaleObjectException' in str(ex)


def _retry_click(
    phone: Device,
    *,
    click_timeout: float = 5,
    max_retries: int = 8,
    retry_delay: float = 0.4,
    pre_wait: float = 3,
    **selector_kw,
) -> None:
    for attempt in range(max_retries):
        try:
            obj = phone(**selector_kw)
            if not obj.wait(timeout=pre_wait):
                raise UiObjectNotFoundError(str(selector_kw))
            obj.click(timeout=click_timeout)
            return
        except UiObjectNotFoundError:
            raise
        except Exception as ex:
            if not _is_stale_error(ex) or attempt >= max_retries - 1:
                raise
            logging.warning('StaleObject，重试 %s/%s: %s', attempt + 1, max_retries, ex)
            time.sleep(retry_delay)


# 跳过教程
def skip_tutorial(phone: Device) -> bool:
    steps: list[tuple[str, dict | str]] = [
        ('眼镜软件版本', 'back'),
        ('开启眼镜自动更新', {
            'description': '开启自动更新',
            'className': 'android.widget.Button',
            'clickable': True,
        }),
        ('数据隐私', {
            'description': '继续',
            'className': 'android.widget.Button',
            'clickable': True,
        }),
        ('唤醒语音助手', {
            'description': '开启语音助手',
            'className': 'android.widget.Button',
            'clickable': True,
        }),
        ('在眼镜上查看通知', {'description': '下一步', 'clickable': True}),
        ('开启眼镜数据分析', {'description': '共享数据', 'clickable': True}),
        ('开始探索', {'description': '开始', 'clickable': True}),
        ('检查仪表板和零件', {'description': '继续', 'clickable': True}),
        ('从应用程序列表启动', {'description': '继续', 'clickable': True}),
        ('回到仪表板', {'description': '继续', 'clickable': True}),
        ('向上浏览控制面板', {'description': '继续', 'clickable': True}),
        ('快捷启动功能', {'description': '继续', 'clickable': True}),
        ('教程完成！', {'description': '启用', 'clickable': True}),
    ]
    for idx, (title, action) in enumerate(steps):
        page_timeout = 5.0 if idx == 0 else 15.0
        if not phone(descriptionContains=title).wait(timeout=page_timeout):
            continue
        if action == 'back' and title == '眼镜软件版本':
            phone.press('back')
            break
        else:
            time.sleep(1)
            _retry_click(phone, packageName=_VENUS_PKG, **action)
        logging.info('跳过教程: %s', title)
    return True


# 打开系统蓝牙设置
def enter_bluetooth_settings(phone: Device) -> bool:
    phone.app_stop('com.android.settings')
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
    label = '开' if on else '关'
    if get_bluetooth_on(phone) == on:
        logging.info('蓝牙已是%s，跳过点击', label)
        return True

    # 直接用 adb 命令，避免 UI 选择器误点设备名称
    _adb_set_bluetooth(phone, on)
    time.sleep(2)
    return wait_for_bluetooth(phone, on, timeout=timeout)


# 切换蓝牙
def switch_bt(phone: Device) -> bool:
    BT_OPPO_SWITCH_ROW_XPATH = '//*[@text="蓝牙"]/ancestor::*[@clickable="true"][1]'
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
            if el is None:
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


# 回到桌面
def press_home(phone: Device) -> None:
    phone.press('home')
    time.sleep(1)
    logging.info('已按 Home 键回到桌面')


# 后台回连测试核心逻辑
def bt_background_reconnect(
    phone: Device,
    glasses: VenusSerialTool,
    bt_name: str,
    bt_off_wait: int,
    bt_on_wait: int,
    reconnect_timeout: int,
    monitor: LogcatMonitor | None = None,
    glasses_monitor: GlassesConnMonitor | None = None,
) -> None:

    # 1. 清空监控状态（只统计开蓝牙之后的日志）
    if monitor:
        monitor.clear_states()
    if glasses_monitor:
        glasses_monitor.clear_states()

    # 2. 按 Home 键将 APP 退到后台
    press_home(phone)

    # 3. 打开系统蓝牙设置
    enter_bluetooth_settings(phone)

    # 4. 关闭蓝牙
    logging.info('关闭蓝牙...')
    if not ensure_bluetooth(phone, on=False, timeout=25):
        raise Exception('等待蓝牙关超时')

    # 5. 等待指定时间
    logging.info('蓝牙已关闭，等待 %ds...', bt_off_wait)
    time.sleep(bt_off_wait)

    # 6. 开启蓝牙
    logging.info('开启蓝牙...')
    if not ensure_bluetooth(phone, on=True, timeout=40):
        raise Exception('等待蓝牙开超时')

    # 7. 等待指定时间（回连等待）
    logging.info('蓝牙已开启，等待 %ds（回连）...', bt_on_wait)
    time.sleep(bt_on_wait)

    # 8. 按 Home 回到桌面（确保不在设置页）
    press_home(phone)

    # 9. 断言回连成功（仅通过 logcat，不打开 APP）
    if monitor:
        if not monitor.wait_for_snapshot_state('connecting', timeout=reconnect_timeout):
            logging.warning('日志断言失败: Snapshot 未进入 connecting')
        if not monitor.wait_for_snapshot_state('connected', timeout=reconnect_timeout):
            logging.warning('日志断言失败: Snapshot 未进入 connected')

    # 10. 眼镜端断言：rnlink 必须经历 0→1 的断连→重连过程
    if glasses_monitor:
        if not glasses_monitor.wait_for_reconnect(timeout=reconnect_timeout):
            rnlink_seq = glasses_monitor.get_state_sequence()
            raise Exception(
                '眼镜端回连失败: rnlink 未完成 0→1 转换 (状态序列:%s)' % rnlink_seq[-10:]
            )
        logging.info('眼镜端 rnlink 完成 0→1 断连重连')

    # 最终判断：Snapshot 状态必须包含 connected
    if monitor and monitor.wait_for_snapshot_state('connected', timeout=reconnect_timeout):
        logging.info('后台回连成功（Snapshot connected）')
    else:
        raise Exception('后台回连失败: Snapshot 未在 %ds 内进入 connected' % reconnect_timeout)


def handle_exception(e: Exception, phone: Device, glasses: VenusSerialTool) -> None:
    logging.warning('失败恢复: %s', e)
    try:
        # 恢复时需要打开 APP 重新连接
        start_venus_app(phone, is_cool_start=True)
        phone.app_stop('com.android.settings')
        time.sleep(5)
    except Exception as ex:
        logging.error('恢复流程启动 Venus 失败: %s', ex)


# 监听系统蓝牙配对弹窗
def _watcher_click_pairing_when_ready(d: Device, selector) -> None:
    el = selector.get_last_match()
    if el is None:
        return
    if str(el.info.get('clickable', 'true')).lower() == 'true':
        el.click()
        rid = el.info.get('resourceName') or el.info.get('resourceId', '')
        logging.info('已点击系统配对 (%s)', rid)


def parse_args():
    parser = argparse.ArgumentParser(description='Android 蓝牙后台回连压测')
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
        '--bt-off-wait', type=int, default=DEFAULT_BT_OFF_WAIT,
        help=f'关闭蓝牙后等待时间（秒） (默认: {DEFAULT_BT_OFF_WAIT})',
    )
    parser.add_argument(
        '--bt-on-wait', type=int, default=DEFAULT_BT_ON_WAIT,
        help=f'开启蓝牙后等待时间（秒） (默认: {DEFAULT_BT_ON_WAIT})',
    )
    parser.add_argument(
        '-t', '--reconnect-timeout', type=int, default=DEFAULT_RECONNECT_TIMEOUT,
        help=f'回连超时时间（秒） (默认: {DEFAULT_RECONNECT_TIMEOUT})',
    )
    parser.add_argument(
        '-c', '--connect-timeout', type=int, default=DEFAULT_BT_CONNECT_TIMEOUT,
        help=f'初始配对连接超时（秒） (默认: {DEFAULT_BT_CONNECT_TIMEOUT})',
    )
    parser.add_argument(
        '-s', '--scan-timeout', type=int, default=DEFAULT_BT_SCAN_TIMEOUT,
        help=f'初始配对扫描超时（秒） (默认: {DEFAULT_BT_SCAN_TIMEOUT})',
    )
    return parser.parse_args()


def main():
    args = parse_args()
    phone_serial = args.phone_serial
    glasses_serial = args.glasses_serial
    test_count = args.test_count
    bt_off_wait = args.bt_off_wait
    bt_on_wait = args.bt_on_wait
    reconnect_timeout = args.reconnect_timeout
    connect_timeout = args.connect_timeout
    scan_timeout = args.scan_timeout

    setup_logging()
    logging.info(
        '测试参数: 手机=%s, 眼镜=%s, 次数=%s, 关蓝牙等待=%ds, 开蓝牙等待=%ds, 回连超时=%ds',
        phone_serial, glasses_serial, test_count, bt_off_wait, bt_on_wait, reconnect_timeout,
    )

    phone, glasses, bt_name = init_device(phone_serial, glasses_serial)

    phone.watcher.when(_SYSTEM_PAIR_DIALOG_XPATH).call(_watcher_click_pairing_when_ready)
    phone.watcher.when('//*[contains(@content-desc,"立即更新") or contains(@text,"立即更新")]').press('back')
    phone.watcher.start(interval=1)

    # 初始连接：先确保眼镜已配对连接
    logging.info('初始连接：确保眼镜已配对连接...')
    start_venus_app(phone, is_cool_start=True)
    switch_to_glasses_tab(phone)
    if not is_connected(phone, bt_name):
        logging.info('眼镜未连接，开始初始配对...')
        glasses.remove_bond()
        enter_connect_page(phone)
        connect_glasses(phone, bt_name, connect_timeout, scan_timeout)
        skip_tutorial(phone)
        switch_to_glasses_tab(phone)
    logging.info('眼镜已连接，开始后台回连压测')

    # 回到桌面，确保 APP 在后台
    press_home(phone)

    monitor = LogcatMonitor(phone.serial, bt_name)
    monitor.start()

    glasses_monitor = GlassesConnMonitor(glasses)
    glasses_monitor.start()

    stats = Statistics()

    pbar = tqdm(
        total=test_count,
        desc='蓝牙后台回连压测',
        unit='轮',
        dynamic_ncols=True,
        bar_format='{desc} {n}/{total} |{bar}| [{elapsed}<{remaining}, {rate_fmt}] {postfix}',
    )
    try:
        with logging_redirect_tqdm():
            for i in range(test_count):
                round_no = i + 1
                pbar.set_description(f'第{round_no}/{test_count}轮')
                logging.info('=================第%s次后台回连开始=================', round_no)
                monitor.clear_states()
                glasses_monitor.clear_states()
                recorder = RoundRecorder(phone.serial, round_no)
                recorder.start()
                try:
                    bt_background_reconnect(
                        phone,
                        glasses,
                        bt_name,
                        bt_off_wait=bt_off_wait,
                        bt_on_wait=bt_on_wait,
                        reconnect_timeout=reconnect_timeout,
                        monitor=monitor,
                        glasses_monitor=glasses_monitor,
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
                    rnlink_seq = glasses_monitor.get_state_sequence()[:] if glasses_monitor else []
                    logging.error(
                        '第%s次后台回连失败：%s%s%s | BLE:%s Bond:%s Snap:%s rnlink:%s',
                        round_no,
                        reason,
                        f' | 截图:{shot}' if shot else ' | 截图:无',
                        f' | 录屏:{rec}' if rec else ' | 录屏:无',
                        ble_last3, bond_last3, snap_last3, rnlink_seq,
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
                    rnlink_seq = glasses_monitor.get_state_sequence()[:] if glasses_monitor else []
                    logging.info(
                        '=================第%s次后台回连成功================= | '
                        '成功:%s 失败:%s 成功率:%.2f%% | '
                        'BLE:%s Bond:%s Snap:%s rnlink:%s',
                        round_no, stats.success_count, stats.failure_count, rate,
                        ble_last3, bond_last3, snap_last3, rnlink_seq,
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
        glasses_monitor.stop()
        stats.print_final_stats()


if __name__ == '__main__':
    main()
