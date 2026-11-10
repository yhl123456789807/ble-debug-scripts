#!/usr/bin/env python3
"""iOS 蓝牙配对压测（facebook-wda + idevicesyslog）。

每轮：解绑(若已连) → 扫描配对 → 跳教程 → 再解绑。对应 Android android-bt-connect.py。
"""
import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import wda
from wda.exceptions import WDAStaleElementReferenceError
from tqdm import tqdm
from tqdm.contrib.logging import logging_redirect_tqdm
from venus_glasses import VenusSerialTool
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.house_arrest import HouseArrestService

# ---------- 可配置常量 ----------
SCRIPT_STEM = Path(__file__).stem
DEFAULT_WDA_URL = 'http://localhost:8100'  # 需先启动 WDA；USB 可用 --udid
DEFAULT_GLASSES_SERIAL = '/dev/tty.usbserial-A9VQ8KI5'
DEFAULT_TEST_COUNT = 1000
DEFAULT_BT_CONNECT_TIMEOUT = 60
DEFAULT_BT_SCAN_TIMEOUT = 35



CONNECT_LIST_SCAN_ROUNDS = 10
PAIR_BTN_REVEAL_TIMEOUT = 20
RECORD_FPS = 2
RECORD_INTERVAL = 1.0 / RECORD_FPS  # 配对列表：滑半屏 + 检查，重复次数
PAIR_BTN_REVEAL_TIMEOUT = 20  # 等待「配对」按钮完整露出后再点
VENUS_BUNDLE = 'com.rayneo.venus.dev'
SETTINGS_BUNDLE = 'com.apple.Preferences'
_SESSION_DIR = Path(__file__).parent / 'logs' / f"{SCRIPT_STEM}_{datetime.now():%Y%m%d_%H%M%S}"
FAIL_CAPTURE_DIR = _SESSION_DIR / 'failure_capture'
PAIR_ALERT_LABELS = ['配对', 'Pair', '好', '确定', '允许', '使用App时允许', '稍后']
# 系统蓝牙「忽略此设备」确认弹窗（实测为 ['忽略设备', '取消']）
FORGET_BT_ALERT_LABELS = ['忽略设备', 'Forget This Device', 'Forget Device', 'Forget', '忽略']

# 只解析 GlassesPairingPage: BLE state changed
BLE_PATTERNS = [
    re.compile(r'GlassesPairingPage: BLE state changed for .* - (\w+)'),
]
# 用于把含关键字的原始行写入调试文件
SYSLOG_KEYWORDS = (
    'GlassesPairingPage: BLE state changed',
)


def _map_ui_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Android description* → WDA label*；packageName 在 iOS 上忽略。"""
    out: dict[str, Any] = {}
    alias = {
        'description': 'label',
        'descriptionContains': 'labelContains',
        'packageName': None,
        'clickable': 'enabled',
        'scrollable': 'visible',
    }
    for key, value in kwargs.items():
        if key == 'className':
            name = value
            if isinstance(name, str) and name.startswith('android.widget.'):
                name = name.rsplit('.', 1)[-1]
            out['className'] = name
            continue
        mapped = alias.get(key, key)
        if mapped:
            out[mapped] = value
    return out


class IosPhone:
    """WDA 薄封装，保持与 Android uiautomator2 脚本相近的调用方式。"""

    def __init__(self, client: wda.Client, bundle_id: str = VENUS_BUNDLE) -> None:
        self.client = client
        self.bundle_id = bundle_id
        self.udid = getattr(client, 'udid', None)
        self.serial = self.udid or 'ios'
        self._session: wda.Client | None = None
        self._wda_url = getattr(client, '_BaseClient__wda_url', DEFAULT_WDA_URL)

    @property
    def session(self) -> wda.Client:
        if self._session is None:
            self._session = self._open_app_session(warm=False)
        return self._session

    def close_session(self) -> None:
        if self._session is None:
            return
        try:
            self._session.close()
        except Exception:
            pass
        self._session = None

    def _open_app_session(self, *, warm: bool) -> wda.Client:
        """创建 WDA session。warm=True 时若 Venus 仍在后台则附着，不 force launch。"""
        always_match: dict[str, Any] = {
            'bundleId': self.bundle_id,
            'arguments': [],
            'environment': {},
            'shouldWaitForQuiescence': False,
            'defaultAlertAction': 'accept',
        }
        if warm:
            always_match.update(
                {
                    'forceAppLaunch': False,
                    'shouldTerminateApp': False,
                    'noReset': True,
                }
            )
        payload = {
            'capabilities': {'alwaysMatch': always_match},
            'desiredCapabilities': dict(always_match),
        }
        root = self.client
        if root.locked():
            root.unlock()
        try:
            res = root.http.post('session', payload)
        except wda.WDAEmptyResponseError:
            res = root.session().app_state(self.bundle_id)
            if res.value != 4:
                raise
            res = root.http.post('session', payload)
        sess = wda.Client(self._wda_url, _session_id=res.sessionId)
        sess._BaseClient__timeout = root._BaseClient__timeout
        sess._BaseClient__callbacks = root._BaseClient__callbacks
        return sess

    def cold_start(self) -> None:
        """仅压测开始时调用一次：杀进程并建立 WDA session。"""
        self.close_session()
        try:
            self.client.app_stop(self.bundle_id)
        except Exception as ex:
            logging.debug('app_stop: %s', ex)
        #time.sleep(2)
        self._session = self._open_app_session(warm=False)

    def ensure_foreground(self) -> None:
        """回到 Venus 前台；复用已有 session，不 POST 新 session、不 app_stop Venus。"""
        if self._session is None:
            self._session = self._open_app_session(warm=True)
        try:
            self.session.app_activate(self.bundle_id)
        except Exception as ex:
            logging.debug('app_activate Venus: %s', ex)
        #time.sleep(0.5)

    def rebind_venus_session(self) -> None:
        """WDA stale 等异常时的兜底：暖附着重建 session。"""
        self.close_session()
        self._session = self._open_app_session(warm=True)
        #time.sleep(0.5)

    def back_from_system_settings(self) -> None:
        """忽略设备后：关设置 App，切回 Venus（不重建 WDA session）。"""
        try:
            self.client.app_stop(SETTINGS_BUNDLE)
        except Exception as ex:
            logging.debug('app_stop Settings: %s', ex)
        self.ensure_foreground()
        if _wait(self, 8, descriptionContains='配对眼镜') or _wait(self, 8, descriptionContains='录音'):
            logging.info('已从系统设置返回 Venus（保留原 session）')
        else:
            logging.warning('返回 Venus 后首页未在 8s 内就绪')

    def screenshot(self, path: str) -> None:
        self.session.screenshot(path)

    def swipe(self, x1: float, y1: float, x2: float, y2: float, steps: int = 0) -> None:
        del steps
        self.session.swipe(x1, y1, x2, y2, 0.35)

    def press(self, key: str) -> None:
        if key == 'back':
            self.client.swipe(0.02, 0.5, 0.4, 0.5, 0.2)
            return
        self.session.press(key)

    def window_size(self) -> tuple[int, int]:
        w, h = self.session.window_size()
        return int(w), int(h)

    def __call__(self, **kwargs: Any) -> wda.Selector:
        return self.session(**_map_ui_kwargs(kwargs))

    def xpath(self, expr: str) -> wda.Selector:
        return self.session.xpath(expr)


class SyslogMonitor:
    """解析 iOS syslog（idevicesyslog / tidevice / go-ios ios syslog）中的连接状态。"""

    def __init__(self, udid: str | None = None) -> None:
        self.udid = udid
        self._proc: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._lock = threading.Lock()
        self._states: dict[str, list[str]] = {'ble': []}
        self._raw_log_path = (
            Path(__file__).parent / 'logs' / SCRIPT_STEM / 'syslog' / 'capture.log'
        )

    def _cmd_env(self) -> tuple[list[str], dict[str, str]]:
        env = os.environ.copy()
        if shutil.which('idevicesyslog'):
            cmd = ['idevicesyslog']
            if self.udid:
                cmd.extend(['-u', self.udid])
            return cmd, env
        if shutil.which('tidevice'):
            cmd = ['tidevice', 'syslog']
            if self.udid:
                cmd.extend(['-u', self.udid])
            return cmd, env
        if shutil.which('ios'):
            env.setdefault('ENABLE_GO_IOS_AGENT', 'user')
            cmd = ['ios', 'syslog']
            return cmd, env
        raise RuntimeError('未找到 idevicesyslog、tidevice 或 ios(go-ios)')

    @staticmethod
    def _normalize_line(line: str) -> str:
        """go-ios 为 JSON 行；idevicesyslog 为「时间 进程: 正文」。"""
        raw = line.strip()
        if not raw:
            return ''
        if raw.startswith('{'):
            try:
                obj = json.loads(raw)
                return str(obj.get('msg') or obj.get('message') or raw)
            except json.JSONDecodeError:
                pass
        if ': ' in raw:
            head, tail = raw.split(': ', 1)
            if tail and not head.endswith('http'):
                return tail
        return raw

    def _maybe_dump_raw(self, text: str) -> None:
        low = text.lower()
        if not any(k.lower() in low for k in SYSLOG_KEYWORDS):
            return
        try:
            self._raw_log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._raw_log_path, 'a', encoding='utf-8') as f:
                f.write(text + '\n')
        except OSError:
            pass

    def _append_state(self, key: str, value: str) -> None:
        """连续相同状态只记一次，便于输出状态迁移路径。"""
        with self._lock:
            seq = self._states[key]
            if seq and seq[-1] == value:
                return
            seq.append(value)

    def _parse_line(self, text: str) -> None:
        for pat in BLE_PATTERNS:
            m = pat.search(text)
            if m:
                self._append_state('ble', m.group(1))
                break

    def start(self) -> None:
        if self._running:
            return
        cmd, env = self._cmd_env()
        if self._raw_log_path.exists():
            self._raw_log_path.unlink()
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1,
            env=env,
        )
        self._running = True
        self._thread = threading.Thread(target=self._read_stream, daemon=True)
        self._thread.start()
        logging.info('SyslogMonitor 已启动: %s', ' '.join(cmd))
        logging.info('原始关键字日志写入: %s', self._raw_log_path)

    def _read_stream(self) -> None:
        if not self._proc or not self._proc.stdout:
            return
        try:
            for line in self._proc.stdout:
                if not self._running:
                    break
                text = self._normalize_line(line)
                if not text:
                    continue
                self._maybe_dump_raw(text)
                self._parse_line(text)
        except Exception as ex:
            logging.error('SyslogMonitor 异常: %s', ex)

    def stop(self) -> None:
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

    def wait_for_ble_state(self, expected: str, timeout: float = 10.0) -> bool:
        return self._wait('ble', expected, timeout)

    def _wait(self, key: str, expected: str, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if expected in self._states[key]:
                    return True
            time.sleep(0.1)
        return False

    def get_state_sequence(self) -> dict:
        with self._lock:
            return {k: list(v) for k, v in self._states.items()}

    def has_state(self, key: str, expected: str) -> bool:
        with self._lock:
            return expected in self._states[key]

    def last_state(self, key: str) -> str | None:
        with self._lock:
            seq = self._states[key]
            return seq[-1] if seq else None

    def log_pairing_diagnostics(self, prefix: str = '配对阶段 ') -> None:
        """配对流程结束后打印 BLE 状态序列。"""
        checks = [
            ('connecting', self.has_state('ble', 'connecting')),
            ('connected', self.has_state('ble', 'connected')),
            ('authSuccess', self.has_state('ble', 'authSuccess')),
        ]
        self.log_missing_checks(checks)
        self.log_captured(prefix)

    def clear_states(self) -> None:
        with self._lock:
            for v in self._states.values():
                v.clear()

    @staticmethod
    def _format_path(states: list[str]) -> str:
        return '→'.join(states) if states else '-'

    def format_summary(self) -> str:
        seq = self.get_state_sequence()
        return f"BLE={self._format_path(seq['ble'])}"

    @staticmethod
    def log_missing_checks(checks: list[tuple[str, bool]]) -> None:
        missing = [name for name, ok in checks if not ok]
        if missing:
            logging.warning('日志诊断未命中: %s', ', '.join(missing))

    def log_captured(self, prefix: str = '') -> None:
        logging.info('%s%s', prefix, self.format_summary())


class Statistics:
    """压测统计；verbose 模式打印最近 BLE/Bond/Snapshot 序列。"""

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
        logging.info('当前已完成轮次: %s', total)
        logging.info('成功次数: %s', self.success_count)
        logging.info('失败次数: %s', self.failure_count)
        logging.info('成功率: %.2f%%', rate)
        if self.failure_reasons:
            counts: dict[str, int] = {}
            for r in self.failure_reasons:
                counts[r] = counts.get(r, 0) + 1
            for reason, cnt in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
                logging.info('  - %s: %s 次', reason, cnt)
            if verbose:
                for d in self.failure_details:
                    seq = d.get('state_sequence') or {}
                    state_line = f"BLE={SyslogMonitor._format_path(seq.get('ble') or [])}"
                    logging.info(
                        '  - 第 %s 轮 @ %s | %s | %s | 截图:%s | 录屏:%s',
                        d['round'],
                        d['time'],
                        d['reason'],
                        state_line,
                        d['screenshot'] or '（无）',
                        d['recording'] or '（无）',
                    )
        logging.info('================================================')

    def print_final_stats(self) -> None:
        self.print_stats(verbose=True)


class RoundRecorder:
    """失败轮次：WDA 后台截帧，OpenCV 合成 mp4；成功轮次删除帧。"""

    def __init__(self, wda_url: str, round_idx: int) -> None:
        self.wda_url = wda_url
        self.round_idx = round_idx
        self._frames_dir: Path | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        try:
            FAIL_CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
            self._frames_dir = FAIL_CAPTURE_DIR / f'_frames_r{self.round_idx}_{datetime.now():%Y%m%d_%H%M%S_%f}'
            self._frames_dir.mkdir(parents=True, exist_ok=True)
            self._stop.clear()
            self._thread = threading.Thread(target=self._capture_loop, daemon=True)
            self._thread.start()
            logging.info('录屏已启动 (WDA 截帧 %sfps)', RECORD_FPS)
            return True
        except Exception as ex:
            logging.warning('录屏启动失败: %s', ex)
            self._frames_dir = None
            return False

    def _capture_loop(self) -> None:
        client = wda.Client(self.wda_url)
        idx = 0
        while not self._stop.wait(RECORD_INTERVAL):
            if not self._frames_dir:
                break
            path = self._frames_dir / f'{idx:05d}.png'
            try:
                client.screenshot(str(path))
                idx += 1
            except Exception as ex:
                logging.debug('录屏截帧失败: %s', ex)

    def _stop_capture(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=15)
            self._thread = None

    def discard(self) -> None:
        self._stop_capture()
        if self._frames_dir and self._frames_dir.exists():
            shutil.rmtree(self._frames_dir, ignore_errors=True)
        self._frames_dir = None

    def save_failure_recording(self) -> str | None:
        self._stop_capture()
        frames_dir = self._frames_dir
        self._frames_dir = None
        if not frames_dir or not frames_dir.exists():
            return None
        pngs = sorted(frames_dir.glob('*.png'))
        if not pngs:
            shutil.rmtree(frames_dir, ignore_errors=True)
            logging.warning('失败录屏无有效截帧')
            return None
        first = cv2.imread(str(pngs[0]))
        if first is None:
            shutil.rmtree(frames_dir, ignore_errors=True)
            logging.warning('失败录屏首帧读取失败')
            return None
        h, w = first.shape[:2]
        dest = FAIL_CAPTURE_DIR / f'fail_r{self.round_idx}_{datetime.now():%Y%m%d_%H%M%S_%f}.mp4'
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        writer = cv2.VideoWriter(str(dest), fourcc, RECORD_FPS, (w, h))
        written = 0
        for p in pngs:
            img = cv2.imread(str(p))
            if img is not None:
                writer.write(img)
                written += 1
        writer.release()
        shutil.rmtree(frames_dir, ignore_errors=True)
        if not dest.exists() or dest.stat().st_size == 0:
            dest.unlink(missing_ok=True)
            logging.warning('失败录屏文件为空')
            return None
        logging.info('失败录屏已保存: %s (%s 帧)', dest, written)
        return str(dest)


def setup_logging() -> None:
    log_dir = _SESSION_DIR / 'phone'
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_dir / 'phone.log', encoding='utf-8'),
            logging.StreamHandler(sys.stderr),
        ],
        force=True,
    )


def connect_wda(wda_url: str | None, udid: str | None) -> wda.Client:
    if udid:
        client = wda.USBClient(udid, port=8100)
    else:
        client = wda.Client(wda_url or DEFAULT_WDA_URL)
    client.wait_ready(timeout=120)
    return client


def init_device(
    wda_url: str | None, udid: str | None, glasses_serial: str
) -> tuple[IosPhone, VenusSerialTool, str, wda.Client]:
    client = connect_wda(wda_url, udid)
    phone = IosPhone(client)
    log_dir = _SESSION_DIR / 'glasses'
    log_dir.mkdir(parents=True, exist_ok=True)
    glasses = VenusSerialTool(com_name=glasses_serial)
    glasses.start_log(str(log_dir))
    bt_name = ''
    while not bt_name:
        bt_name = glasses.get_bt_name()
        logging.info('眼镜蓝牙名称：%s', bt_name)
        time.sleep(1)
    return phone, glasses, bt_name, client


def _wait(phone: IosPhone, timeout: float = 5.0, _retried: bool = False, **kwargs: Any) -> bool:
    try:
        return phone(**kwargs).wait(timeout=timeout) is not None
    except Exception as ex:
        msg = str(ex).lower()
        if ('call depth' in msg or 'stale element' in msg) and not _retried:
            logging.warning('WDA 状态异常，重建 Venus 连接后重试: %s', kwargs)
            phone.rebind_venus_session()
            return _wait(phone, timeout, _retried=True, **kwargs)
        if 'call depth' in msg:
            logging.warning('WDA 查询过深，跳过 wait: %s', kwargs)
            return False
        raise


def _click(phone: IosPhone, timeout: float = 5.0, **kwargs: Any) -> None:
    phone(**kwargs).get(timeout=timeout).click()


def _settings_ui(phone: IosPhone) -> wda.Client:
    """根 WDA client，操作系统设置 UI。"""
    return phone.client


def _retry_click_xpath(
    ui: wda.Client,
    xpath: str,
    *,
    max_retries: int = 10,
    delay: float = 0.5,
) -> None:
    for attempt in range(max_retries):
        try:
            ui.xpath(xpath).click()
            return
        except WDAStaleElementReferenceError as ex:
            if attempt >= max_retries - 1:
                raise
            logging.warning('Stale element，重试 %s/%s: %s', attempt + 1, max_retries, ex)
            time.sleep(delay)


def _selector_exists(sel: wda.Selector) -> bool:
    try:
        return sel.exists
    except Exception as ex:
        if 'call depth' in str(ex).lower():
            return False
        raise


def _sel_wait(sel: wda.Selector, timeout: float = 1.0) -> bool:
    """比 exists 更稳，避免 WDA call depth 误判为不存在。"""
    try:
        return sel.wait(timeout=timeout, raise_error=False) is not None
    except Exception as ex:
        if 'call depth' in str(ex).lower():
            return False
        raise


def ensure_glasses_home(phone: IosPhone, after_settings: bool = False) -> None:
    """轮次间回到眼镜 tab（不启动 App）。"""
    if after_settings:
        phone.back_from_system_settings()
    for _ in range(6):
        if _wait(phone, 2, descriptionContains='配对眼镜') or _wait(phone, 2, descriptionContains='录音'):
            logging.info('已在眼镜 tab')
            return
        if _wait(phone, 2, description='眼镜', className='Button'):
            _click(phone, 3, description='眼镜', className='Button', clickable=True)
            #time.sleep(0.5)
            continue
        phone.press('back')
        time.sleep(0.4)
    switch_to_glasses_tab(phone)


def start_venus_app_once(phone: IosPhone) -> None:
    """压测开始前仅调用一次。"""
    phone.cold_start()
    time.sleep(8)
    logging.info('Venus app 冷启动完成')
    try:
        phone.session.appium_settings({'boundElementsByIndex': True})
        logging.info('WDA boundElementsByIndex 已开启')
    except Exception as ex:
        logging.warning('boundElementsByIndex 未生效（可忽略）: %s', ex)
    if _wait(phone, 5, descriptionContains='配对眼镜') or _wait(phone, 5, descriptionContains='RayNeo AI'):
        logging.info('Venus app 就绪')
        return
    raise RuntimeError('Venus app 启动失败')


def switch_to_glasses_tab(phone: IosPhone) -> None:
    if _wait(phone, 3, descriptionContains='配对眼镜') or _wait(phone, 3, descriptionContains='录音'):
        logging.info('已在眼镜 tab')
        return
    for _ in range(8):
        if _wait(phone, 2, description='眼镜', className='Button'):
            _click(phone, 5, description='眼镜', className='Button', clickable=True)
            #time.sleep(0.5)
            if _wait(phone, 3, descriptionContains='配对眼镜') or _wait(phone, 3, descriptionContains='录音'):
                logging.info('切换到眼镜 tab 成功')
                return
        phone.press('back')
        #time.sleep(0.4)
    raise RuntimeError('切换到眼镜 tab 失败')


def is_connected(phone: IosPhone, bt_name: str) -> bool:
    if _wait(phone, 2, descriptionContains='配对眼镜'):
        logging.info('眼镜未连接')
        return False
    if _wait(phone, 2, descriptionContains=bt_name):
        logging.info('眼镜已连接')
        return True
    return True


def enter_venus_settings(phone: IosPhone) -> None:
    phone.swipe(0.5, 0.8, 0.5, 0.2)
    if _wait(phone, 5, descriptionContains='设置'):
        _click(phone, 5, descriptionContains='设置')
    if _wait(phone, 10, descriptionContains='设置'):
        logging.info('进入 Venus 设置成功')
        return
    raise RuntimeError('进入 Venus 设置失败')


def open_system_bluetooth_settings(phone: IosPhone) -> wda.Client:
    """解绑后手动打开系统设置 → 蓝牙（Venus 无「前往设置」弹窗）。"""
    root = _settings_ui(phone)
    try:
        root.app_stop(SETTINGS_BUNDLE)
        root.app_launch(SETTINGS_BUNDLE, wait_for_quiescence=False)
    except TypeError:
        root.app_launch(SETTINGS_BUNDLE)
    logging.info('已 launch 系统设置')
    time.sleep(1)
    if root(labelContains='蓝牙').wait(timeout=8):
        root(labelContains='蓝牙').click()
    elif root(nameContains='Bluetooth').wait(timeout=5):
        root(nameContains='Bluetooth').click()
    else:
        root.swipe(0.5, 0.75, 0.5, 0.35, 0.3)
        #time.sleep(0.5)
        if not root(labelContains='蓝牙').click_exists(timeout=5):
            if not root(nameContains='Bluetooth').click_exists(timeout=5):
                raise RuntimeError('打开系统蓝牙设置页面失败')
    #time.sleep(1)
    if not (
        root(labelContains='蓝牙').wait(timeout=5)
        or root(nameContains='Bluetooth').wait(timeout=3)
    ):
        raise RuntimeError('打开系统蓝牙设置页面失败')
    logging.info('已打开系统蓝牙设置页面')
    return root


def forget_ios_bluetooth_device(phone: IosPhone, bt_name: str) -> None:
    """App 解绑后：手动进设置 → 蓝牙 → 更多信息 → 忽略此设备。"""
    settings = open_system_bluetooth_settings(phone)
    try:
        _forget_ios_bluetooth_device_on_settings(phone, settings, bt_name)
    finally:
        phone.back_from_system_settings()


def _forget_ios_bluetooth_device_on_settings(
    phone: IosPhone, settings: wda.Client, bt_name: str
) -> None:
    if not settings(labelContains=bt_name).wait(timeout=15):
        for _ in range(3):
            settings.swipe(0.5, 0.7, 0.5, 0.3)
            #time.sleep(0.5)
            if settings(labelContains=bt_name).wait(timeout=3):
                break
        else:
            logging.info('系统蓝牙列表中无 %s，跳过忽略设备', bt_name)
            return

    bt_esc = bt_name.replace('"', '\\"')
    info_xpaths = [
        (
            '//XCUIElementTypeCell['
            f'.//XCUIElementTypeStaticText['
            f'@label="{bt_esc}" or @name="{bt_esc}" or @value="{bt_esc}"'
            ']'
            ']'
            '//XCUIElementTypeButton[@label="更多信息" or @name="更多信息"]'
        ),
        f'//XCUIElementTypeCell[contains(@label,"{bt_name}")]'
        f'//XCUIElementTypeButton[@label="更多信息"]',
        '//XCUIElementTypeButton[@label="更多信息"]',
    ]
    clicked_info = False
    for xp in info_xpaths:
        btn = settings.xpath(xp)
        if _selector_exists(btn):
            try:
                _retry_click_xpath(settings, xp)
            except WDAStaleElementReferenceError:
                btn.click()
            clicked_info = True
            logging.info('已点击「更多信息」(xpath)')
            break
    if not clicked_info:
        for kw in ({'label': '更多信息'}, {'name': '更多信息'}):
            btn = settings(**kw)
            if _selector_exists(btn):
                btn.click()
                clicked_info = True
                logging.info('已点击「更多信息」')
                break
    if not clicked_info:
        raise RuntimeError(f'系统蓝牙页未找到 {bt_name} 的「更多信息」按钮')

    #time.sleep(1)
    forgot = False
    for label in ('忽略此设备', '忽略设备', 'Forget This Device', 'Forget Device'):
        cell = settings(label=label)
        if cell.wait(timeout=5):
            cell.click()
            forgot = True
            logging.info('已点击「%s」', label)
            break
    if not forgot:
        raise RuntimeError('设备详情页未找到「忽略此设备」')

    #time.sleep(0.8)
    try:
        buttons = phone.client.alert.buttons()
        logging.info('忽略设备确认弹窗: %s', buttons)
        for text in FORGET_BT_ALERT_LABELS:
            for btn_text in buttons:
                if text in btn_text:
                    phone.client.alert.click(btn_text)
                    logging.info('已确认忽略: %s', btn_text)
                    forgot = True
                    break
            else:
                continue
            break
    except Exception as ex:
        logging.debug('无系统确认弹窗或已自动关闭: %s', ex)

    #time.sleep(0.5)
    logging.info('系统蓝牙已忽略设备: %s', bt_name)


def disconnect_glasses(
    phone: IosPhone,
    bt_name: str,
    monitor: SyslogMonitor | None = None,
) -> None:
    scroll = phone(className='ScrollView', visible=True)
    if scroll.wait(timeout=2):
        try:
            for _ in range(8):
                scroll.scroll('down')
        except Exception:
            phone.swipe(0.5, 0.75, 0.5, 0.28)
            logging.info('toEnd 失败，改用手势滑动')
    else:
        phone.swipe(0.5, 0.75, 0.5, 0.28)
        logging.info('toEnd 失败，改用手势滑动')
    time.sleep(0.2)
    if _wait(phone, 5, descriptionContains='解除绑定'):
        _click(phone, 15, descriptionContains='解除绑定', clickable=True)
    if _wait(phone, 5, descriptionContains='是否解除绑定？'):
        _click(phone, 5, description='解除绑定', clickable=True)
        logging.info('App 内解除绑定成功')
        if _wait(phone, 0.5, descriptionContains='网络'):
            raise RuntimeError('解绑失败,请连接网络后重试')
        #time.sleep(1)
        forget_ios_bluetooth_device(phone, bt_name)


def enter_connect_page(phone: IosPhone) -> None:
    if _wait(phone, 5, description='配对眼镜'):
        _click(phone, 5, description='配对眼镜', clickable=True)
    if (
        _wait(phone, 10, descriptionContains='正在搜索')
        or _wait(phone, 5, descriptionContains='Found the following glasses')
        or _wait(phone, 5, descriptionContains='请选择你的眼镜')
        or _wait(phone, 5, descriptionContains='找不到你的眼镜')
    ):
        logging.info('进入连接页面成功')
        time.sleep(1)  # 等设备渲染；轻滑检查在 connect_glasses 内进行
        return
    #raise RuntimeError('进入连接页面失败')


def _log_visible_devices(phone: IosPhone, bt_name: str) -> None:
    names: list[str] = []
    for sel in (phone(labelContains='Venus'), phone(nameContains='Venus')):
        try:
            if not _selector_exists(sel):
                continue
            el = sel.get()
            lb = (el.label or el.name or '').strip()
            if lb and lb not in names:
                names.append(lb)
        except Exception:
            pass
    if names:
        logging.error('当前屏可见设备: %s；目标: %s', ', '.join(names), bt_name)
    else:
        logging.error('当前屏未解析到 Venus 设备名，目标: %s', bt_name)


def _find_pair_btn_for_target(phone: IosPhone, bt_name: str) -> tuple[int, int] | None:
    """找到目标设备名同卡片的「配对」按钮中心坐标。

    页面结构（XML dump 验证）：每个设备卡片是一个 XCUIElementTypeOther，
    label 包含设备名（如 "Venus-413"）。配对按钮(Button label="配对")是
    该 Other 的 following-sibling。不可见的按钮 width/height=0。
    """
    bt_esc = bt_name.replace('"', '\\"')
    xpaths = (
        # 精确：卡片 Other → 同级配对按钮
        f'//XCUIElementTypeOther[@label="{bt_esc}"]/following-sibling::XCUIElementTypeButton[@label="配对"]',
        f'//XCUIElementTypeOther[contains(@label,"{bt_esc}")]/following-sibling::XCUIElementTypeButton[@label="配对"]',
        # 兜底：父级内任意配对按钮
        f'//XCUIElementTypeOther[contains(@label,"{bt_esc}")]/..//XCUIElementTypeButton[@label="配对"]',
    )
    for xp in xpaths:
        try:
            for el in phone.xpath(xp).find_elements():
                b = el.bounds
                if b.width > 0 and b.height > 0 and b.y > 0:
                    return int(b.x + b.width // 2), int(b.y + b.height // 2)
        except Exception:
            continue
    return None


def _footer_top(phone: IosPhone) -> int:
    """列表底部遮挡区域的上边界 y 坐标。"""
    top = phone.window_size()[1]
    for desc in ('找不到你的眼镜', '重新搜索'):
        sel = phone(labelContains=desc)
        if _selector_exists(sel):
            try:
                top = min(top, sel.get().bounds.y)
            except Exception:
                pass
    return top


def connect_glasses(
    phone: IosPhone,
    bt_name: str,
    bt_connect_timeout: int,
    bt_scan_timeout: int,
    monitor: SyslogMonitor | None = None,
) -> None:
    """在配对列表中滚动查找 bt_name，确保「配对」按钮不被遮挡后点击。"""
    deadline = time.monotonic() + bt_scan_timeout

    # 阶段1：滚动扫描，找到目标设备的配对按钮
    center = None
    for scan_i in range(CONNECT_LIST_SCAN_ROUNDS):
        if time.monotonic() >= deadline:
            logging.warning('扫描超时 (%ss)', bt_scan_timeout)
            break
        center = _find_pair_btn_for_target(phone, bt_name)
        if center is not None:
            logging.info('第 %d/%d 次扫描：已定位 %s 配对按钮', scan_i + 1, CONNECT_LIST_SCAN_ROUNDS, bt_name)
            break
        logging.info('第 %d/%d 次扫描：未定位 %s', scan_i + 1, CONNECT_LIST_SCAN_ROUNDS, bt_name)
        if scan_i + 1 < CONNECT_LIST_SCAN_ROUNDS:
            phone.session.swipe(0.45, 0.68, 0.45, 0.38, 0.28)
            time.sleep(0.5)
            if not (_wait(phone, 1, descriptionContains='找不到你的眼镜')
                    or _wait(phone, 1, descriptionContains='请选择你的眼镜')
                    or _wait(phone, 1, descriptionContains='Found the following glasses')):
                logging.warning('滑半屏后离开配对页，重新进入')
                enter_connect_page(phone)

    if center is None:
        center = _find_pair_btn_for_target(phone, bt_name)
    if center is None:
        _log_visible_devices(phone, bt_name)
        raise RuntimeError(f'连接眼镜失败: 列表中未找到目标设备 {bt_name}')

    # 阶段2：确保按钮不被遮挡，必要时滚动露出
    _, win_h = phone.window_size()
    safe_top = int(win_h * 0.20)
    reveal_deadline = time.monotonic() + PAIR_BTN_REVEAL_TIMEOUT
    while time.monotonic() < reveal_deadline:
        # 重新获取坐标（滚动后可能变化）
        center = _find_pair_btn_for_target(phone, bt_name) or center
        cx, cy = center
        ft = _footer_top(phone)
        safe_bottom = ft - 20
        if safe_top <= cy <= safe_bottom:
            break
        # 被底部遮挡 → 下滚让内容上移；被顶部遮挡 → 上滚
        if cy > safe_bottom:
            logging.info('配对按钮被底部遮挡(y=%s, footer=%s)，下滚露出', cy, ft)
            phone.session.swipe(0.45, 0.58, 0.45, 0.42, 0.25)
        else:
            phone.session.swipe(0.45, 0.42, 0.45, 0.58, 0.25)
        time.sleep(0.5)
    else:
        try:
            FAIL_CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
            shot = FAIL_CAPTURE_DIR / f'pair_btn_blocked_{datetime.now():%Y%m%d_%H%M%S}.png'
            phone.screenshot(str(shot))
            logging.error('配对按钮未完全露出，截图: %s', shot)
        except Exception:
            pass
        raise RuntimeError('连接眼镜失败：配对按钮未在时限内完整露出')

    # 阶段3：点击配对按钮，验证 BLE 状态
    cx, cy = center
    for attempt in range(3):
        phone.session.click(cx, cy)
        logging.info('已点击配对按钮 (%s, %s)', cx, cy)
        if monitor:
            time.sleep(2)
            if monitor.has_state('ble', 'connecting') or monitor.has_state('ble', 'connected'):
                break
            # 重新获取坐标重试
            new_center = _find_pair_btn_for_target(phone, bt_name)
            if new_center:
                cx, cy = new_center
            logging.warning('点击后 BLE 无变化，重试 %s/3', attempt + 1)
        else:
            break

    logging.info('点击配对成功')
    ble_ok = bool(monitor and monitor.wait_for_ble_state('authSuccess', timeout=bt_connect_timeout))
    if ble_ok:
        if monitor:
            monitor.log_pairing_diagnostics()
        time.sleep(1)
        logging.info('眼镜配对成功')
        return
    if monitor:
        monitor.log_pairing_diagnostics()
    raise RuntimeError('眼镜配对失败：未检测到 BLE authSuccess')


def skip_tutorial(phone: IosPhone) -> None:
    """配对后引导：每轮都跑；无教程页时短超时快速跳过（对齐 Android）。"""
    steps: list[tuple[str, dict[str, Any]]] = [
        ('眼镜软件升级', {'description': '暂不更新'}),
        ('开启眼镜自动更新', {'description': '开启自动更新', 'className': 'Button'}),
        ('数据隐私', {'description': '继续', 'className': 'Button'}),
        ('唤醒语音助手', {'description': '开启语音助手', 'className': 'Button'}),
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
    for idx, (title, click_kw) in enumerate(steps):
        # 首屏可能加载慢；后续轮次无教程时用短超时避免卡住
        step_timeout = 10.0 if idx == 0 else 3.0
        if not _wait(phone, step_timeout, descriptionContains=title):
            continue
        if click_kw.get('action') == 'back':
            phone.press('back')
        else:
            _click(phone, 5, **click_kw)
        logging.info('跳过教程: %s', title)


def after_pairing_go_home(phone: IosPhone) -> None:
    """配对成功：过教程（若有）并回到眼镜 tab。"""
    time.sleep(1)
    skip_tutorial(phone)
    switch_to_glasses_tab(phone)


def pair_glasses(
    phone: IosPhone,
    glasses: VenusSerialTool,
    bt_name: str,
    bt_connect_timeout: int,
    bt_scan_timeout: int,
    monitor: SyslogMonitor | None = None,
) -> None:
    """单轮配对流程；App 仅在压测开始前启动一次。"""
    ensure_glasses_home(phone, after_settings=False)
    if is_connected(phone, bt_name):
        enter_venus_settings(phone)
        disconnect_glasses(phone, bt_name, monitor)
    enter_connect_page(phone)
    connect_glasses(phone, bt_name, bt_connect_timeout, bt_scan_timeout, monitor)
    after_pairing_go_home(phone)
    enter_venus_settings(phone)
    disconnect_glasses(phone, bt_name, monitor)


def pull_app_logs(
    bundle_id: str = VENUS_BUNDLE,
    sandbox_subdir: str = 'Documents/appLog',
    dest_dir: Path | None = None,
    serial: str | None = None,
) -> Path:
    if dest_dir is None:
        dest_dir = _SESSION_DIR / 'phone' / 'app_logs'
    dest_dir.mkdir(parents=True, exist_ok=True)
    logging.info('拉取 APP 沙盒日志: bundle=%s, 子路径=%s', bundle_id, sandbox_subdir)
    try:
        lockdown = create_using_usbmux(serial=serial)
        ha = HouseArrestService(lockdown=lockdown, bundle_id=bundle_id)
        ha.pull(sandbox_subdir, str(dest_dir), progress_bar=False)
        ha.close()
        lockdown.close()
        logging.info('APP 沙盒日志已保存到: %s', dest_dir)
    except Exception:
        logging.exception('拉取 APP 沙盒日志失败')
    return dest_dir


class AssertAlertWatcher:
    """后台线程持续检测含「RayneoNet 断言」的弹窗，检测到后自动拉取 APP 沙盒日志。"""

    POLL_INTERVAL = 0.5

    def __init__(self, phone: wda.Client) -> None:
        self._phone = phone
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._triggered = False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._triggered = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logging.info('AssertAlertWatcher 已启动')

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        logging.info('AssertAlertWatcher 已停止')

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                el = self._phone(labelContains='RayneoNet 断言')
                if el.exists:
                    popup_text = el.text or ''
                    logging.warning('检测到断言弹窗: %s', popup_text)
                    self._triggered = True
                    pull_app_logs()
                    logging.info('断言弹窗已检测，日志已拉取')
            except Exception:
                pass
            self._stop_event.wait(self.POLL_INTERVAL)


def handle_exception(phone: IosPhone, glasses: VenusSerialTool, bt_name: str) -> None:
    """失败后清眼镜绑定、系统忽略设备并冷启动 App。"""
    pull_app_logs()
    glasses.remove_bond()
    try:
        forget_ios_bluetooth_device(phone, bt_name)
    except Exception as ex:
        logging.warning('系统忽略设备失败: %s', ex)
    try:
        start_venus_app_once(phone)
        time.sleep(2)
    except Exception as ex:
        raise RuntimeError(f'启动 Venus app 失败: {ex}') from ex


def save_failure_screenshot(phone: IosPhone, round_idx: int) -> str | None:
    try:
        FAIL_CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        path = FAIL_CAPTURE_DIR / f'fail_r{round_idx}_{datetime.now():%Y%m%d_%H%M%S_%f}.png'
        phone.screenshot(str(path))
        logging.info('失败截图已保存: %s', path)
        return str(path)
    except Exception as ex:
        logging.warning('保存失败截图异常: %s', ex)
        return None


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description='iOS 蓝牙配对压测 (facebook-wda)')
    p.add_argument('--wda-url', default=DEFAULT_WDA_URL, help='WDA 地址')
    p.add_argument('--udid', default=None, help='设备 UDID')
    p.add_argument('-g', '--glasses-serial', default=DEFAULT_GLASSES_SERIAL, help='眼镜串口')
    p.add_argument('-n', '--test-count', type=int, default=DEFAULT_TEST_COUNT, help='测试轮次')
    p.add_argument('-c', '--connect-timeout', type=int, default=DEFAULT_BT_CONNECT_TIMEOUT, help='连接超时')
    p.add_argument('-s', '--scan-timeout', type=int, default=DEFAULT_BT_SCAN_TIMEOUT, help='扫描超时')
    return p.parse_args()


def main() -> None:
    """压测主循环；开始前冷启动一次 App，失败时 handle_exception 再冷启动。"""
    args = parse_args()
    setup_logging()
    logging.info(
        '测试参数: wda=%s udid=%s 眼镜=%s 次数=%s 连接超时=%s 扫描超时=%s',
        args.wda_url,
        args.udid,
        args.glasses_serial,
        args.test_count,
        args.connect_timeout,
        args.scan_timeout,
    )
    phone, glasses, bt_name, client = init_device(args.wda_url, args.udid, args.glasses_serial)
    glasses.remove_bond()
    monitor = SyslogMonitor(args.udid)
    monitor.start()
    stats = Statistics()
    start_venus_app_once(phone)
    alert_watcher = AssertAlertWatcher(client)
    alert_watcher.start()
    pbar = tqdm(total=args.test_count, desc='iOS 蓝牙配对压测', unit='轮', dynamic_ncols=True)
    try:
        with logging_redirect_tqdm():
            with client.alert.watch_and_click(PAIR_ALERT_LABELS + FORGET_BT_ALERT_LABELS, interval=0.1):
                # 后台自动处理系统蓝牙配对、权限等 Alert
                for i in range(args.test_count):
                    round_no = i + 1
                    pbar.set_description(f'第{round_no}/{args.test_count}轮')
                    logging.info('=================第%s次配对开始=================', round_no)
                    monitor.clear_states()
                    recorder = RoundRecorder(args.wda_url, round_no)
                    if not recorder.start():
                        logging.warning('第%s轮录屏未启动', round_no)
                    try:
                        pair_glasses(
                            phone,
                            glasses,
                            bt_name,
                            bt_connect_timeout=args.connect_timeout,
                            bt_scan_timeout=args.scan_timeout,
                            monitor=monitor,
                        )
                    except Exception as e:
                        try:
                            reason = str(e)
                            shot = save_failure_screenshot(phone, round_no)
                            rec = recorder.save_failure_recording()
                            stats.record_failure(round_no, reason, shot, rec, monitor.get_state_sequence())
                            logging.error(
                                '第%s次配对失败：%s%s%s',
                                round_no,
                                reason,
                                f' | 截图:{shot}' if shot else ' | 截图:无',
                                f' | 录屏:{rec}' if rec else ' | 录屏:无',
                            )
                            stats.print_stats(verbose=False)
                            handle_exception(phone, glasses, bt_name)
                        except Exception as e:
                            pass

                    else:
                        stats.record_success()
                        recorder.discard()
                        logging.info('=================第%s次配对成功=================', round_no)
                        stats.print_stats(verbose=False)
                    done = stats.success_count + stats.failure_count
                    rate = stats.success_count / done * 100 if done else 0.0
                    pbar.set_postfix(成功=stats.success_count, 失败=stats.failure_count, 成功率=f'{rate:.1f}%')
                    pbar.update(1)
    except KeyboardInterrupt:
        logging.info('收到键盘中断，结束压测')
    finally:
        pbar.close()
        alert_watcher.stop()
        monitor.stop()
        stats.print_final_stats()


if __name__ == '__main__':
    main()
