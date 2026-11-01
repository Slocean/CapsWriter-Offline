# coding: utf-8
"""
网页语音模型管理（下载 / 校验 / 删除）

边界（用户 2026-10-03 确认）：
- 仅 CapsWriter 语音识别模型（fun_asr_nano / qwen_asr q4_k·q5_k / sensevoice /
  paraformer）及其 ASR 必需的标点、对齐辅助文件；不做上传、不做通用文本 LLM；
- 每套模型文件合计上限 5,000,000,000 字节（十进制 5GB），下载与解压两个阶段
  都按真实流字节数强制执行，不信任 HEAD / Content-Length / ZIP 元数据；
- 下载只允许本模块固定清单内的官方 GitHub Release 资产（名称、字节、SHA-256
  逐一钉死），不接受任意 URL；重定向仅允许 GitHub 官方域名，拒绝内网/回环/
  未知主机；禁止下载可执行文件与压缩包内的符号链接/reparse 条目；
- 所有落盘路径从 Cfg.asr_work_dir 下的 models 根目录推导（兼容线上 models
  目录为 junction 的部署），发布/删除目标必须是清单内叶子目录且经 realpath
  归一化后严格位于 models 根内部；绝不递归删除 models 根或其他目录；
- 正在运行（或运行态未知）的模型：拒绝删除、拒绝下载覆盖；fail-closed；
- 阶段目录下载+完整 SHA-256 校验+解压校验后才原子发布（同卷 rename），
  失败/取消/进程重启不会把半成品误报为已安装（安装状态始终来自磁盘事实）。

清单来源：GitHub API repos/HaujetZhao/CapsWriter-Offline/releases/tags/models
（2026-10-03 读取的 asset name / size / sha256 digest），独立复核可用：
  curl -s https://api.github.com/repos/HaujetZhao/CapsWriter-Offline/releases/tags/models
"""

from __future__ import annotations

import errno
import fnmatch
import hashlib
import json
import os
import shutil
import stat
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import audit
from .config_admin import AdminConfig as Cfg
from .ops_gate import OPS_GATE

# ---------------- 固定清单（官方资产，钉死名称/字节/SHA-256） ----------------

CAP_BYTES = 5_000_000_000

RELEASE_PAGE = 'https://github.com/HaujetZhao/CapsWriter-Offline/releases/tag/models'
_ASSET_BASE = 'https://github.com/HaujetZhao/CapsWriter-Offline/releases/download/models/'

# 生产下载策略：仅 https + GitHub 官方域名（含其资产 CDN 子域）。
# 测试用目录可以通过传入自定义 catalog 覆盖（逻辑同一条代码路径）。
PRODUCTION_POLICY = {'scheme': 'https', 'host_globs': ('github.com', '*.githubusercontent.com')}

_EXECUTABLE_SUFFIXES = frozenset({
    '.exe', '.dll', '.bat', '.cmd', '.ps1', '.scr', '.com', '.pif', '.msi',
    '.vbs', '.vbe', '.js', '.jse', '.wsf', '.wsh', '.jar', '.hta', '.cpl', '.sys',
})


def _asset(name: str, size: int, sha256: str, extract_into: str, strip_prefix: Optional[str],
           required: Tuple[str, ...], purpose: str, kind: str = 'main',
           flat_names: Optional[Tuple[str, ...]] = None,
           variant_marker: Optional[Dict[str, Any]] = None,
           installed_size: Optional[int] = None) -> Dict[str, Any]:
    return {
        'asset': name,
        'url': _ASSET_BASE + name,
        'size': size,
        'sha256': sha256,
        'installed_size': installed_size,   # 展开后字节数（官方资产调查值，供磁盘预估/展示）
        'extract_into': extract_into,
        'strip_prefix': strip_prefix,
        'flat_names': flat_names,
        'required': required,       # 相对叶子目录的必需文件（与 check_model 同源语义）
        'purpose': purpose,
        'kind': kind,               # main=识别主模型  aux=ASR 辅助模型
        'variant_marker': variant_marker,
    }


def _set(label: str, model_type: str, description: str, components: List[Dict[str, Any]],
         variant: Optional[str] = None) -> Dict[str, Any]:
    return {
        'label': label, 'model_type': model_type, 'description': description,
        'variant': variant, 'components': components,
    }


_VARIANT_MARKER_QWEN = {'file': 'qwen3_asr_llm.gguf',
                        'sizes': {'q4_k': 1282434624, 'q5_k': 1471800896}}

MODEL_CATALOG: Dict[str, Any] = {
    'cap_bytes': CAP_BYTES,
    'policy': PRODUCTION_POLICY,
    'release_page': RELEASE_PAGE,
    'max_files_per_zip': 10_000,
    'sets': {
        'fun_asr_nano': _set(
            'Fun-ASR-Nano', 'fun_asr_nano',
            'GGUF 语音解码器 + ONNX 编码器/CTC，自带标点与时间戳，准确率最高。',
            [_asset(
                'Fun-ASR-Nano-GGUF.zip', 834231292,
                '26a557923aedc44f1a3033d0a9b9c7b13cbb551f57fb9fd4b15a67bb4b57f998',
                'Fun-ASR-Nano', 'Fun-ASR-Nano-GGUF',
                ('Fun-ASR-Nano-Encoder-Adaptor.fp16.onnx',
                 'Fun-ASR-Nano-CTC.fp16.onnx',
                 'Fun-ASR-Nano-Decoder.q5_k.gguf',
                 'tokens.txt'),
                '识别主模型（内部 GGUF 解码器是语音模型组成部分）',
                kind='main', installed_size=987732408,
            )],
        ),
        'sensevoice': _set(
            'SenseVoice-Small', 'sensevoice',
            'ONNX 推理，多语言（中英日韩粤），自带标点与时间戳，体积最小。',
            [_asset(
                'Sensevoice-Small-ONNX.zip', 433798984,
                '3948b5761f12db1c01d7a7e596294b43b0316aa5c7a8df77981e78573997dcbb',
                'SenseVoice-Small', 'Sensevoice-Small-ONNX',
                ('SenseVoice-Encoder.fp16.onnx',
                 'SenseVoice-CTC.fp16.onnx',
                 'tokenizer.bpe.model'),
                '识别主模型（自带标点）', installed_size=473542751,
            )],
        ),
        'paraformer': _set(
            'Paraformer', 'paraformer',
            'sherpa-onnx 推理，自带时间戳；无标点能力，需外挂标点模型（一并下载）。',
            [
                _asset(
                    'Paraformer.zip', 239979687,
                    'a12a3f9791483329441c94ad759cbcf258d7246784a6d368cd3c591add4d888b',
                    'Paraformer',
                    'speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-onnx',
                    ('model.onnx', 'tokens.txt'),
                    '识别主模型（无标点能力，需搭配标点模型）', installed_size=259883915,
                ),
                _asset(
                    'Punct-CT-Transformer.zip', 271770396,
                    'de106e6cf13764bd3124f31864bc30158f04961788765b63262bdd5ba21fa421',
                    'Punct-CT-Transformer',
                    'sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12',
                    ('model.onnx',),
                    '标点辅助模型（仅用于 ASR：Paraformer 引擎缺少标点能力时自动外挂）',
                    kind='aux', installed_size=298589419,
                ),
            ],
        ),
        'qwen_asr_q4_k': _set(
            'Qwen3-ASR（q4_k 量化）', 'qwen_asr',
            'GGUF 版 Qwen3-ASR 1.7B，自带标点；q4_k 体积更小。与 q5_k 共享同一运行目录，互斥安装。',
            [
                _asset(
                    'Qwen3-ASR-1.7B-q4_k.zip', 1410584449,
                    '9b3d2a66a4a26a0404c32085ec838b7c482495a7827919a5aa674de617c2757b',
                    'Qwen3-ASR/Qwen3-ASR-1.7B', None,
                    ('qwen3_asr_encoder_frontend.onnx',
                     'qwen3_asr_encoder_backend.onnx',
                     'qwen3_asr_llm.gguf'),
                    '识别主模型（压缩包为平铺布局，直接展开进运行目录）',
                    installed_size=1468051775,
                    flat_names=('qwen3_asr_encoder_frontend.onnx',
                                'qwen3_asr_encoder_backend.onnx',
                                'qwen3_asr_llm.gguf'),
                    variant_marker=_VARIANT_MARKER_QWEN,
                ),
                _asset(
                    'Qwen3-ForcedAligner-0.6B.zip', 626205170,
                    '8fa035db150e1621a01cbb313bf152b593818b0f15dd94180452f22fc77fbf43',
                    'Qwen3-ForcedAligner', 'Qwen3-ForcedAligner-0.6B',
                    ('qwen3_aligner_encoder_frontend.int4.onnx',
                     'qwen3_aligner_encoder_backend.int4.onnx',
                     'qwen3_aligner_llm.q5_k.gguf'),
                    '对齐辅助模型（仅用于 ASR：Qwen3-ASR 无时间戳能力，文件转录的时间戳对齐使用）',
                    kind='aux', installed_size=813396362,
                ),
            ],
            variant='q4_k',
        ),
        'qwen_asr_q5_k': _set(
            'Qwen3-ASR（q5_k 量化）', 'qwen_asr',
            'GGUF 版 Qwen3-ASR 1.7B，自带标点；q5_k 精度更高、体积更大。与 q4_k 共享同一运行目录，互斥安装。',
            [
                _asset(
                    'Qwen3-ASR-1.7B-q5_k.zip', 1951570656,
                    'f40040fe62a5ef0c09f8699fdbcb30f18bb8ae2bcd515ed4954e1f62b8b0e88f',
                    'Qwen3-ASR', 'Qwen3-ASR-1.7B',
                    ('qwen3_asr_encoder_frontend.onnx',
                     'qwen3_asr_encoder_backend.onnx',
                     'qwen3_asr_llm.gguf'),
                    '识别主模型', installed_size=2107141427,
                    variant_marker=_VARIANT_MARKER_QWEN,
                ),
                _asset(
                    'Qwen3-ForcedAligner-0.6B.zip', 626205170,
                    '8fa035db150e1621a01cbb313bf152b593818b0f15dd94180452f22fc77fbf43',
                    'Qwen3-ForcedAligner', 'Qwen3-ForcedAligner-0.6B',
                    ('qwen3_aligner_encoder_frontend.int4.onnx',
                     'qwen3_aligner_encoder_backend.int4.onnx',
                     'qwen3_aligner_llm.q5_k.gguf'),
                    '对齐辅助模型（仅用于 ASR：文件转录时间戳对齐）',
                    kind='aux', installed_size=813396362,
                ),
            ],
            variant='q5_k',
        ),
    },
}


class ModelOpError(Exception):
    """携带用户可读原因的操作失败（转成 4xx 或任务失败原因）"""


class TaskCancelled(Exception):
    """任务被用户取消"""


# ---------------- 路径与安全工具 ----------------

def _norm(path_str: str) -> str:
    return os.path.normcase(os.path.realpath(str(path_str)))


def models_root() -> Path:
    """models 根目录：从 asr_work_dir 推导（与 ASR 进程 cwd 语义一致）"""
    return Path(Cfg.asr_work_dir) / 'models'


def host_allowed(host: str, policy: Dict[str, Any]) -> bool:
    """
    下载主机白名单：仅当主机名匹配策略 glob 时放行。生产策略的 glob 全部是
    GitHub 官方域名，IP 字面量/内网地址天然不匹配而被拒绝；测试策略可以
    显式写明 '127.0.0.1' 走同一条校验路径。
    """
    host = (host or '').strip().lower().rstrip('.')
    if not host:
        return False
    return any(fnmatch.fnmatch(host, pat.lower()) for pat in policy.get('host_globs', ()))


def _validate_url(url: str, policy: Dict[str, Any]) -> Tuple[str, str]:
    """校验清单内/重定向目标 URL：scheme 与主机都必须符合策略；拒绝用户凭据"""
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    if parts.scheme.lower() != policy.get('scheme', 'https'):
        raise ModelOpError(f'拒绝非 {policy.get("scheme", "https")} 下载地址: {url[:120]}')
    if '@' in (parts.netloc or ''):
        raise ModelOpError('拒绝携带用户凭据的下载地址')
    host = (parts.hostname or '').lower()
    if not host_allowed(host, policy):
        raise ModelOpError(f'下载主机不在允许清单内: {host or "(空)"}')
    return host, url


class _RedirectGate(urllib.request.HTTPRedirectHandler):
    """重定向逐跳校验：允许 GitHub 官方资产/CDN 跳转，拒绝内网与未知主机"""

    max_redirections = 5

    def __init__(self, policy: Dict[str, Any]):
        super().__init__()
        self._policy = policy

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _, newurl = _validate_url(newurl, self._policy)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _safe_rel_path(name: str) -> Optional[str]:
    """zip 条目名 → 安全相对路径；非法（绝对/穿越/反斜杠/NUL/盘符）返回 None。
    目录条目的尾部分隔符合法（如 'Wrapper/'），返回去掉尾斜杠的路径。"""
    if not name or '\\' in name or '\x00' in name:
        return None
    if name.startswith('/') or (len(name) > 1 and name[1] == ':'):
        return None
    name = name.rstrip('/')
    if not name:
        return None
    parts = name.split('/')
    if any(p in ('', '.', '..') for p in parts):
        return None
    return '/'.join(parts)


def _entry_is_symlink(info: zipfile.ZipInfo) -> bool:
    return ((info.external_attr >> 16) & 0o170000) == 0o120000


def _entry_mode_unsafe(info: zipfile.ZipInfo) -> bool:
    """有 unix 权限位且不是常规文件/目录 → 拒绝（设备文件、fifo 等）"""
    mode = (info.external_attr >> 16) & 0o170000
    if mode == 0:
        return False
    return mode not in (0o100000, 0o40000)


def _assert_no_reparse(dir_abs: Path) -> None:
    """目录内出现符号链接/junction/reparse 点一律拒绝递归删除（fail-closed）"""
    reparse_flag = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
    for root, dirs, files in os.walk(dir_abs):
        for name in list(dirs) + list(files):
            p = os.path.join(root, name)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if getattr(st, 'st_file_attributes', 0) & reparse_flag or os.path.islink(p):
                raise ModelOpError(f'检测到符号链接/junction 条目，拒绝递归处理: {p}')


def _dir_size(dir_abs: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(dir_abs):
        for name in files:
            try:
                st = os.lstat(os.path.join(root, name))
            except OSError:
                continue
            if not getattr(st, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400):
                total += st.st_size
    return total


def _file_present(base: Path, rel: str) -> bool:
    p = base / rel
    try:
        return p.is_file()
    except OSError:
        return False


# ---------------- 模型管理器 ----------------

class ModelManager:
    """
    网页语音模型管理：清单查询、下载任务（单任务）、删除。

    安装状态永远以磁盘事实为准（必需文件存在性），任务状态只是过程信息——
    进程重启/失败后不会把模型误报为已安装。下载任务全程持有共享操作闸门，
    与 ASR 启停、删除互斥。
    """

    # 专用暂存根：目录名标明本功能所有权。根内每个本模块任务目录在创建成功
    # 后写入归属 marker（.cw-model-task.json，含 owner/task_id）；一切清理
    # （启动恢复、任务结束、创建失败路径）都只针对"marker 可验证归属 + 非
    # reparse + realpath 在 models 根内"三者同时成立的目录——绝不只凭目录名
    # 断定归属，绝不清理本任务未成功创建的目录。models 根允许是 junction
    # （线上部署形态），但专用暂存根与任务目录本身不允许 reparse/逃逸。
    STAGING_DIRNAME = '.cw-model-staging'
    STAGE_MARKER_NAME = '.cw-model-task.json'
    STAGE_OWNER = 'capswriter-admin-model-manager'

    def __init__(self, asr=None, catalog: Optional[Dict[str, Any]] = None):
        self._lock = threading.RLock()
        self._asr = asr
        self._catalog = catalog or MODEL_CATALOG
        self._task: Optional[Dict[str, Any]] = None
        self._cancel = threading.Event()
        self._sweep_staging()

    # ---------- 基础 ----------

    @property
    def cap_bytes(self) -> int:
        return int(self._catalog.get('cap_bytes', CAP_BYTES))

    def _root(self) -> Path:
        return models_root()

    def _leaf_abs(self, component: Dict[str, Any]) -> Path:
        rel = component['extract_into']
        if component.get('strip_prefix'):
            rel = f"{rel}/{component['strip_prefix']}"
        return self._root() / rel

    def _stage_dir(self) -> Path:
        return self._root() / self.STAGING_DIRNAME

    def _policy(self) -> Dict[str, Any]:
        return self._catalog.get('policy', PRODUCTION_POLICY)

    def _is_own_task_name(self, name: str) -> bool:
        """本模块创建的任务目录名：start_download 生成的 12 位小写十六进制 ID。
        仅用于初筛；真正归属判定必须再过 _verify_stage_ownership 的 marker 校验。"""
        return len(name) == 12 and all(c in '0123456789abcdef' for c in name)

    # ---------- 暂存区统一校验（写入前/清理前唯一依据） ----------

    @staticmethod
    def _entry_is_reparse(path: Path) -> bool:
        reparse_flag = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
        try:
            st = os.lstat(path)
        except OSError:
            return False
        return bool(getattr(st, 'st_file_attributes', 0) & reparse_flag) or os.path.islink(path)

    def _assert_stage_containment(self, path: Path) -> None:
        """realpath 必须严格位于 models 根（realpath）内部——models 根允许是
        junction（部署形态），但暂存路径本身经 junction 逃逸出 models 根则拒绝"""
        root_real = _norm(self._root())
        path_real = _norm(path)
        if path_real == root_real or not path_real.startswith(root_real + os.sep):
            raise ModelOpError(f'暂存路径不在 models 根目录内部: {path}')

    def _assert_stage_area_ok(self) -> Path:
        """
        任何暂存写入前的统一校验：专用暂存根若是符号链接/junction/普通文件，
        或 realpath 逃逸出 models 根，一律拒绝（models 根自身的 junction 合法，
        realpath 比较两侧都会解析）。返回暂存根路径。
        """
        stage = self._stage_dir()
        if os.path.lexists(stage):
            if self._entry_is_reparse(stage):
                raise ModelOpError('专用暂存根是符号链接/junction，拒绝写入或清理')
            if not stage.is_dir():
                raise ModelOpError('专用暂存根被非目录对象占用，拒绝写入')
        self._assert_stage_containment(stage)
        return stage

    def _write_stage_marker(self, stage_root: Path, task_id: str, model: str) -> None:
        """任务目录创建成功后立即写入归属 marker（清理的唯一归属凭据）"""
        marker = stage_root / self.STAGE_MARKER_NAME
        marker.write_text(json.dumps({
            'owner': self.STAGE_OWNER,
            'task_id': task_id,
            'model': model,
            'pid': os.getpid(),
            'created_at': time.time(),
        }, ensure_ascii=False), encoding='utf-8')

    def _verify_stage_ownership(self, path: Path, task_id: str) -> bool:
        """
        目录归属判定（清理前必须通过）：非 reparse 的真实目录、realpath 严格
        在专用暂存根内部、内部归属 marker 的 owner/task_id 与本次清理对象一致。
        任一不成立即不算本模块任务目录，一律不清理。
        """
        stage_real = _norm(self._stage_dir())
        try:
            if self._entry_is_reparse(path) or not path.is_dir():
                return False
            if _norm(path) != _norm(self._stage_dir() / path.name):
                return False
            if not _norm(path).startswith(stage_real + os.sep):
                return False
            marker = path / self.STAGE_MARKER_NAME
            if not marker.is_file():
                return False
            data = json.loads(marker.read_text(encoding='utf-8'))
            return (data.get('owner') == self.STAGE_OWNER
                    and data.get('task_id') == task_id
                    and task_id == path.name)
        except (OSError, ValueError):
            return False

    def _sweep_staging(self) -> None:
        """
        启动恢复清扫：只清理【归属可验证】的本模块遗留任务目录——12 位任务名
        仅作初筛，必须再通过 _verify_stage_ownership（marker/非 reparse/包含
        关系），删除前再对目录内部整体做 reparse 扫描。暂存根若是 junction/
        符号链接则整体跳过（绝不透过它清理外部数据）；无 marker 的同名目录
        （其他程序或旧版遗留）一律保留。
        """
        stage = self._stage_dir()
        if not os.path.lexists(stage):
            return
        if self._entry_is_reparse(stage):
            audit.record('models.staging_sweep_skipped', ok=False,
                         detail='暂存根是符号链接/junction，拒绝清理')
            return
        if not stage.is_dir():
            return
        removed = kept = 0
        for child in list(stage.iterdir()):
            if not self._is_own_task_name(child.name):
                kept += 1
                continue
            if not self._verify_stage_ownership(child, child.name):
                kept += 1
                continue
            try:
                _assert_no_reparse(child)
                shutil.rmtree(child)
                removed += 1
            except (OSError, ModelOpError):
                kept += 1
        if removed or kept:
            audit.record('models.staging_swept',
                         detail=f'removed={removed} kept_unowned={kept}')
        try:
            if not any(stage.iterdir()):
                stage.rmdir()
        except OSError:
            pass

    # ---------- 状态查询 ----------

    def _selected_model_type(self) -> str:
        from config_server import ServerConfig as ServerConfigClass
        from .settings_store import load_settings
        try:
            value = load_settings().get('model_type')
        except Exception:
            value = None
        if value:
            return str(value)
        return str(getattr(ServerConfigClass, 'model_type', '') or '')

    def _asr_state(self) -> Dict[str, Any]:
        try:
            st = self._asr.status() if self._asr is not None else None
        except Exception:
            st = None
        if not st:
            return {'state': 'unknown', 'running_model_type': None, 'detail_model_type': None}
        detail = st.get('detail') or {}
        running = detail.get('model_type') or None
        return {
            'state': st.get('state'),
            'running_model_type': running,
            'detail_model_type': running,
        }

    def protected_model_types(self, asr_info: Optional[Dict[str, Any]] = None) -> set:
        """
        运行保护集合：online/starting 时=实际运行模型（控制通道不可知时退化为
        设置选中值）；stale/unknown（控制通道不可用、状态无法核实）= 全部模型
        ——fail-closed：不能推断已停止就允许破坏。
        """
        info = asr_info or self._asr_state()
        state = info.get('state')
        if state in ('online', 'starting'):
            running = info.get('running_model_type') or self._selected_model_type()
            return {running} if running else set()
        if state in ('stale', 'unknown', None):
            return {s['model_type'] for s in self._catalog['sets'].values()}
        return set()

    def _component_status(self, component: Dict[str, Any]) -> Dict[str, Any]:
        leaf = self._leaf_abs(component)
        exists = leaf.is_dir()
        present = {rel: (_file_present(leaf, rel)) for rel in component['required']}
        missing = [rel for rel, ok in present.items() if not ok]
        installed = exists and not missing
        variant = None
        marker = component.get('variant_marker')
        if marker and installed:
            f = leaf / marker['file']
            try:
                size = f.stat().st_size
                for name, expected in marker['sizes'].items():
                    if size == expected:
                        variant = name
                        break
            except OSError:
                variant = None
        return {
            'name': component['asset'],
            'kind': component.get('kind', 'main'),
            'purpose': component['purpose'],
            'leaf': str(leaf.relative_to(self._root())) if exists else (
                component['extract_into'] + (f"/{component['strip_prefix']}" if component.get('strip_prefix') else '')),
            'exists': exists,
            'installed': installed,
            'size_bytes': _dir_size(leaf) if exists else 0,
            'required_missing': missing,
            'variant': variant,
            'asset': {'name': component['asset'], 'size': component['size'],
                      'sha256': component['sha256'], 'url': component['url']},
        }

    def _set_block_reasons(self, info: Dict[str, Any], protected: set, task_active: bool) -> None:
        model_type = info['model_type']
        if info['complete']:
            # 仅整套完整才拒绝下载；主模型就绪但缺辅助组件时允许"仅补缺失组件"
            info['download_block'] = '已完整安装；如需重装请先删除'
        elif model_type in protected:
            info['download_block'] = ('该模型正在运行（或运行状态无法核实），禁止下载覆盖；'
                                      '请先安全停止/切换后再操作')
        elif task_active:
            info['download_block'] = '已有下载任务进行中'
        else:
            info['download_block'] = None
        if model_type in protected:
            info['delete_block'] = ('该模型正在运行（或运行状态无法核实），禁止删除；'
                                    '请先安全停止，或切换到其他已安装模型')
        elif task_active:
            info['delete_block'] = '已有下载任务进行中，稍后再试'
        else:
            info['delete_block'] = None

    def status_payload(self) -> Dict[str, Any]:
        with self._lock:
            task = dict(self._task) if self._task else None
            task_active = bool(task and task.get('state') in ('pending', 'running'))
        asr_info = self._asr_state()
        protected = self.protected_model_types(asr_info)
        selected = self._selected_model_type()
        root = self._root()
        models = []
        for key, spec in self._catalog['sets'].items():
            components = [self._component_status(c) for c in spec['components']]
            main = next((c for c in components if c['kind'] == 'main'), components[0])
            set_complete = all(c['installed'] for c in components)
            main_ready = bool(main['installed'])
            size = sum(c['size_bytes'] for c in components)
            variant = next((c['variant'] for c in components if c['variant']), None)
            info = {
                'key': key,
                'label': spec['label'],
                'model_type': spec['model_type'],
                'variant': spec.get('variant'),
                'description': spec['description'],
                # 整套完整 = 全部组件（含必需的标点/对齐辅助）都已安装；
                # 主模型就绪 = 仅主组件完整，可切换、可补下载缺失的辅助组件
                'complete': set_complete,
                'main_ready': main_ready,
                'switchable': main_ready,
                'missing_components': [c['name'] for c in components if not c['installed']],
                'selected': selected == spec['model_type'],
                'running': spec['model_type'] in protected,
                'installed_variant': variant,
                'size_bytes': size,
                'cap_bytes': self.cap_bytes,
                'components': components,
            }
            self._set_block_reasons(info, protected, task_active)
            models.append(info)
        free = None
        try:
            free = shutil.disk_usage(str(root if root.exists() else Path(Cfg.asr_work_dir))).free
        except OSError:
            free = None
        return {
            'ok': True,
            'models_root': str(root),
            'models_root_exists': root.exists(),
            'release_page': self._catalog.get('release_page', RELEASE_PAGE),
            'cap_bytes': self.cap_bytes,
            'free_bytes': free,
            'asr_state': asr_info.get('state'),
            'selected_model_type': selected,
            'protected_model_types': sorted(p for p in protected if p),
            'task': self._task_view(task),
            'models': models,
        }

    # ---------- 下载任务 ----------

    def current_task(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return dict(self._task) if self._task else None

    def forget_finished_task(self) -> None:
        """显式忘记一个已结束（done/failed/cancelled）的任务展示；
        pending/running 的任务绝不清除（那会掩盖仍在运行的下载）。"""
        with self._lock:
            t = self._task
            if t and t.get('state') in ('done', 'failed', 'cancelled'):
                self._task = None

    @staticmethod
    def _task_view(task: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not task:
            return None
        view = {k: task.get(k) for k in (
            'id', 'model', 'label', 'state', 'stage', 'asset_index', 'asset_count',
            'asset_name', 'bytes_received', 'bytes_total', 'message', 'error',
            'submitted_at', 'finished_at')}
        return view

    def _update_task(self, task: Dict[str, Any], **fields: Any) -> None:
        with self._lock:
            if self._task is task:
                task.update(fields)

    def _check_cancel(self, task: Dict[str, Any]) -> None:
        if self._cancel.is_set():
            raise TaskCancelled()

    def start_download(self, key: str, ip: str = '') -> Dict[str, Any]:
        spec = self._catalog['sets'].get(key)
        if spec is None:
            raise ModelOpError(f'未知的模型标识: {key}')
        owner = f'model-download-{key}'
        with self._lock:
            if self._task and self._task.get('state') in ('pending', 'running'):
                return {'ok': False, 'error': 'conflict',
                        'detail': f"已有下载任务进行中（{self._task.get('model')}）"}
        if not OPS_GATE.acquire(owner):
            return {'ok': False, 'error': 'conflict',
                    'detail': f'其他操作正在进行中（{OPS_GATE.owner}），稍后再试'}
        started = False
        task = None
        try:
            status = self.status_payload()
            info = next(m for m in status['models'] if m['key'] == key)
            if info['download_block']:
                return {'ok': False, 'error': 'not_allowed', 'detail': info['download_block']}
            # 只补缺失的组件：已完整的组件一律跳过、绝不覆盖（取消后重下同样
            # 从缺失处继续）；既有组件的字节计入整套 5GB 预算
            pending = [c for c in spec['components'] if not self._component_installed(c)]
            if not pending:
                return {'ok': False, 'error': 'not_allowed', 'detail': '已完整安装；如需重装请先删除'}
            if not self._check_disk_space(pending):
                return {'ok': False, 'error': 'not_allowed',
                        'detail': '磁盘剩余空间不足（需要 下载包+展开体积+余量），请清理后再试'}
            try:
                # 暂存区写入前统一校验（暂存根为 junction/符号链接/逃逸 → 提交即拒绝）
                self._assert_stage_area_ok()
            except ModelOpError as e:
                return {'ok': False, 'error': 'not_allowed', 'detail': str(e)}
            budget_base = sum(self._existing_component_bytes(c) for c in spec['components'])
            task = {
                'id': uuid.uuid4().hex[:12],
                'model': key,
                'label': spec['label'],
                'state': 'pending',
                'stage': '排队中',
                'asset_index': 0,
                'asset_count': len(pending),
                'asset_name': '',
                'bytes_received': 0,
                'bytes_total': sum(c['size'] for c in pending),
                'budget_base': budget_base,
                'published_bytes': 0,
                'message': '',
                'error': '',
                'submitted_at': time.time(),
                'finished_at': None,
            }
            self._cancel.clear()
            with self._lock:
                self._task = task
            audit.record('model.download', ip,
                         detail=f'key={key} task={task["id"]} components={len(pending)}/{len(spec["components"])}')
            t = threading.Thread(target=self._run_download, args=(task, spec, pending, owner),
                                 daemon=True, name=f'model-dl-{key}')
            try:
                t.start()
            except Exception:
                with self._lock:
                    if self._task is task:
                        self._task = None
                raise
            started = True
            return {'ok': True, 'task': self._task_view(task)}
        finally:
            if not started:
                # 预检拒绝或线程没跑起来：闸门与任务占位由这里释放；
                # 线程跑起来后由 _run_download 的 finally 释放闸门
                OPS_GATE.release(owner)
                with self._lock:
                    if self._task is task:
                        self._task = None

    def _component_installed(self, component: Dict[str, Any]) -> bool:
        leaf = self._leaf_abs(component)
        return leaf.is_dir() and all(_file_present(leaf, rel) for rel in component['required'])

    def _existing_component_bytes(self, component: Dict[str, Any]) -> int:
        """该组件叶子目录当前在磁盘上的字节数（不存在的目录计 0）"""
        leaf = self._leaf_abs(component)
        return _dir_size(leaf) if leaf.exists() else 0

    def _check_disk_space(self, components: list) -> bool:
        root = self._root()
        anchor = str(root if root.exists() else Path(Cfg.asr_work_dir))
        need = 512 * 1024 * 1024
        for c in components:
            installed = c.get('installed_size')
            need += int(c['size']) + (int(installed) if installed else int(c['size']) * 2)
        try:
            return shutil.disk_usage(anchor).free > need
        except OSError:
            return False

    def cancel_download(self, ip: str = '') -> Dict[str, Any]:
        with self._lock:
            task = self._task
            if not task or task.get('state') not in ('pending', 'running'):
                return {'ok': False, 'error': 'conflict', 'detail': '当前没有进行中的下载任务'}
        self._cancel.set()
        audit.record('model.download.cancel', ip, detail=f"key={task['model']} task={task['id']}")
        return {'ok': True, 'task': self._task_view(task)}

    def _run_download(self, task: Dict[str, Any], spec: Dict[str, Any],
                      pending: list, owner: str) -> None:
        """
        下载任务线程。暂存区写入遵循统一所有权流程：

        1. 任何写入前先 _assert_stage_area_ok（暂存根为 junction/符号链接/
           逃逸出 models 根 → 任务失败，绝不落盘）；
        2. 任务目录 mkdir(exist_ok=False)：已存在（非本任务创建）→ 任务失败，
           且 finally 绝不清理它；
        3. 只有本任务成功创建的任务目录才写归属 marker 并参与清理；finally
           清理前再走 _verify_stage_ownership + 内部 reparse 扫描——校验、
           创建与清理用同一套判定，没有"初始化一套、finally 另一套"。
        """
        stage_root = self._stage_dir() / task['id']
        stage_created = False
        try:
            self._update_task(task, state='running', stage='准备目录…')
            stage_area = self._assert_stage_area_ok()
            try:
                stage_area.mkdir(parents=True, exist_ok=True)
            except FileExistsError:
                pass
            self._assert_stage_area_ok()   # 创建后复核（防创建窗口内被替换）
            try:
                stage_root.mkdir(exist_ok=False)
            except FileExistsError:
                raise ModelOpError(
                    f'任务暂存目录已存在且非本任务创建，拒绝复用或清理: {stage_root}')
            stage_created = True
            self._write_stage_marker(stage_root, task['id'], task['model'])
            for idx, component in enumerate(pending):
                self._check_cancel(task)
                # 组件可能在任务期间被并行补齐：装前重查，已完整则跳过不覆盖
                if self._component_installed(component):
                    self._update_task(
                        task, stage='组件已存在，跳过',
                        message=f"{component['asset']} 已完整，跳过")
                    continue
                self._install_component(task, component, stage_root / f'c{idx}', idx)
            self._update_task(task, state='done', stage='完成', message='安装完成',
                              finished_at=time.time())
            audit.record('model.download.done', detail=f"key={task['model']} task={task['id']}")
        except TaskCancelled:
            self._update_task(task, state='cancelled', stage='已取消',
                              message='已取消；已完成的组件保留，重新下载只补缺失组件',
                              finished_at=time.time())
            audit.record('model.download.cancelled', ok=True,
                         detail=f"key={task['model']} task={task['id']}")
        except Exception as e:  # noqa: BLE001
            self._update_task(task, state='failed', stage='失败',
                              error=str(e)[:500], finished_at=time.time())
            audit.record('model.download.failed', ok=False,
                         detail=f"key={task['model']} task={task['id']} err={str(e)[:120]}")
        finally:
            if stage_created:
                # 只清理本任务创建且归属可验证的目录（内部出现 reparse 一并拒绝）
                if self._verify_stage_ownership(stage_root, task['id']):
                    try:
                        _assert_no_reparse(stage_root)
                        shutil.rmtree(stage_root)
                    except (OSError, ModelOpError) as e:
                        audit.record('model.download.cleanup_skipped', ok=False,
                                     detail=f'task={task["id"]} err={str(e)[:120]}')
                else:
                    audit.record('model.download.cleanup_skipped', ok=False,
                                 detail=f'task={task["id"]} 归属校验未通过，保留现场')
                try:
                    # 顺带收走空暂存根（有其他内容时保留不动）
                    self._stage_dir().rmdir()
                except OSError:
                    pass
            else:
                audit.record('model.download.staging_not_created',
                             detail=f"task={task['id']} 未成功创建暂存目录，不做任何清理")
            OPS_GATE.release(owner)
            threading.Thread(target=self._expire_task_later, args=(task,), daemon=True).start()

    def _expire_task_later(self, task: Dict[str, Any]) -> None:
        time.sleep(300)
        with self._lock:
            if self._task is task:
                self._task = None

    # ---------- 单组件：下载 → 校验 → 解压 → 原子发布 ----------

    def _install_component(self, task: Dict[str, Any], component: Dict[str, Any],
                           stage: Path, idx: int) -> None:
        self._update_task(task, asset_index=idx, asset_name=component['asset'],
                          stage='下载中', bytes_received=0)
        zip_path = stage / 'asset.zip'
        stage.mkdir(parents=True, exist_ok=True)
        self._download_and_verify(task, component, zip_path,
                                  budget_used=self._set_bytes_so_far(task))
        self._check_cancel(task)

        leaf_abs = self._leaf_abs(component)
        self._validate_leaf_target(leaf_abs)

        self._update_task(task, stage='解压校验中…')
        stage_leaf = self._extract_zip(task, component, zip_path, stage,
                                       budget_used=self._set_bytes_so_far(task))
        self._check_cancel(task)

        # 发布前复查：目标已变完整（并行修改）→ fail-closed 中止，不动现有目录
        if leaf_abs.is_dir():
            missing = [rel for rel in component['required'] if not _file_present(leaf_abs, rel)]
            if not missing:
                raise ModelOpError('目标目录已完整安装（可能被并行修改），中止发布以避免覆盖')

        self._update_task(task, stage='发布安装…')
        old_bytes = self._existing_component_bytes(component)   # 残目录将被替换
        self._publish(stage_leaf, leaf_abs, stage)
        with self._lock:
            if self._task is task:
                task['published_bytes'] = (int(task.get('published_bytes', 0))
                                           + _dir_size(leaf_abs) - old_bytes)
        self._update_task(task, stage='组件完成',
                          message=f"{component['asset']} 已安装")

    def _set_bytes_so_far(self, task: Dict[str, Any]) -> int:
        """整套模型当前口径字节 = 任务起点既有组件字节 + 本任务净新增字节。

        发布前的校验预算包含被替换残目录的旧字节（保守，只会更早触发上限）；
        发布后按 新叶字节-旧叶字节 累计，口径回到真实磁盘事实。
        """
        with self._lock:
            return int(task.get('budget_base', 0)) + int(task.get('published_bytes', 0))

    def _download_and_verify(self, task: Dict[str, Any], component: Dict[str, Any],
                             dest: Path, budget_used: int = 0) -> None:
        policy = self._policy()
        _, url = _validate_url(component['url'], policy)
        opener = urllib.request.build_opener(_RedirectGate(policy))
        req = urllib.request.Request(url, headers={
            'User-Agent': 'CapsWriter-Admin/2.6 (model-management)',
            'Accept': 'application/octet-stream',
        })
        expected_size = int(component['size'])
        expected_sha = str(component['sha256']).lower()
        received = 0
        digest = hashlib.sha256()
        try:
            with opener.open(req, timeout=30) as resp:
                code = getattr(resp, 'status', None) or resp.getcode()
                if code != 200:
                    raise ModelOpError(f'下载响应异常: HTTP {code}')
                with open(dest, 'wb') as out:
                    while True:
                        self._check_cancel(task)
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        received += len(chunk)
                        # 真实流字节硬限额：超过整套上限（含既有组件占用）、
                        # 官方资产大小，即中止
                        if (received > self.cap_bytes
                                or received > expected_size
                                or budget_used + received > self.cap_bytes):
                            raise ModelOpError(
                                f'下载字节数超过限制（已收 {received}，官方资产 {expected_size}，'
                                f'整套预算 {budget_used}），已中止')
                        digest.update(chunk)
                        out.write(chunk)
                        self._update_task(task, bytes_received=received)
        except urllib.error.HTTPError as e:
            raise ModelOpError(f'下载失败: HTTP {e.code}') from e
        except urllib.error.URLError as e:
            raise ModelOpError(f'下载连接失败: {e.reason}') from e
        if received != expected_size:
            raise ModelOpError(f'下载字节数与官方资产不符（{received} ≠ {expected_size}）')
        actual_sha = digest.hexdigest()
        if actual_sha != expected_sha:
            raise ModelOpError(f'SHA-256 校验失败（{actual_sha[:16]}… ≠ 官方 {expected_sha[:16]}…）')

    def _validate_leaf_target(self, leaf_abs: Path) -> None:
        """叶子目录 realpath 必须严格位于 models 根内部；本身不得是 reparse 点"""
        root_real = _norm(self._root())
        leaf_real = _norm(leaf_abs)
        if leaf_real == root_real:
            raise ModelOpError('拒绝操作 models 根目录本身')
        try:
            common = os.path.commonpath([leaf_real, root_real])
        except ValueError as e:
            raise ModelOpError('路径不可比较（盘符不同）') from e
        if common != root_real or not leaf_real.startswith(root_real + os.sep):
            raise ModelOpError(f'目标路径不在 models 根目录内部: {leaf_abs}')
        try:
            st = os.lstat(leaf_abs)
            if getattr(st, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400):
                raise ModelOpError(f'目标是符号链接/junction，拒绝覆盖: {leaf_abs}')
        except FileNotFoundError:
            pass

    def _extract_zip(self, task: Dict[str, Any], component: Dict[str, Any],
                     zip_path: Path, stage: Path, budget_used: int = 0) -> Path:
        """
        解压到 stage（不接触安装目录）。先整体校验中央目录（路径安全、布局
        匹配、无符号链接/可执行文件、总量预算），再流式解出（真实字节计数、
        逐条 CRC 校验），任何异常都不产生部分发布。
        """
        strip = component.get('strip_prefix')
        flat_names = component.get('flat_names')
        cap = self.cap_bytes
        out_dir = stage / 'x'
        out_dir.mkdir(parents=True, exist_ok=True)
        plan: List[Tuple[zipfile.ZipInfo, Path]] = []
        total_usize = 0
        with zipfile.ZipFile(zip_path) as zf:
            infos = zf.infolist()
            if len(infos) > int(self._catalog.get('max_files_per_zip', 10_000)):
                raise ModelOpError('压缩包条目数超过限制，疑似压缩炸弹')
            for info in infos:
                rel = _safe_rel_path(info.filename)
                if rel is None:
                    raise ModelOpError(f'压缩包含非法路径条目: {info.filename[:80]}')
                if _entry_is_symlink(info):
                    raise ModelOpError(f'压缩包含符号链接条目，拒绝: {info.filename[:80]}')
                if _entry_mode_unsafe(info):
                    raise ModelOpError(f'压缩包含非常规文件条目，拒绝: {info.filename[:80]}')
                if strip is not None:
                    if rel == strip:
                        continue  # 顶层目录条目本身
                    if not rel.startswith(strip + '/'):
                        raise ModelOpError(f'压缩包布局与固定清单不符: {info.filename[:80]}')
                    rel_under_leaf = rel[len(strip) + 1:]
                else:
                    if flat_names and rel not in flat_names:
                        raise ModelOpError(f'压缩包文件不在固定清单内: {info.filename[:80]}')
                    rel_under_leaf = rel
                suffix = os.path.splitext(rel_under_leaf)[1].lower()
                if suffix in _EXECUTABLE_SUFFIXES:
                    raise ModelOpError(f'压缩包含可执行文件，拒绝: {info.filename[:80]}')
                if info.is_dir():
                    continue
                total_usize += int(info.file_size)
                if budget_used + total_usize > cap:
                    raise ModelOpError(f'展开后整套模型将超过 {cap} 字节上限，已中止')
                plan.append((info, out_dir / rel))
            extracted = 0
            out_real = _norm(out_dir)
            for info, target in plan:
                self._check_cancel(task)
                target.parent.mkdir(parents=True, exist_ok=True)
                real_parent = _norm(target.parent)
                if real_parent != out_real and not real_parent.startswith(out_real + os.sep):
                    raise ModelOpError('解压路径越界')
                written = 0
                with zf.open(info) as src, open(target, 'wb') as dst:
                    while True:
                        chunk = src.read(256 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        extracted += len(chunk)
                        if budget_used + extracted > cap:
                            raise ModelOpError('解压真实字节超过整套上限，已中止（压缩炸弹防护）')
                        dst.write(chunk)
                if written != int(info.file_size):
                    raise ModelOpError(f'解压字节数与清单不符: {info.filename[:80]}')
        stage_leaf = out_dir / strip if strip else out_dir
        if not stage_leaf.is_dir():
            raise ModelOpError('压缩包缺少预期的顶层目录')
        return stage_leaf

    def _publish(self, stage_leaf: Path, leaf_abs: Path, stage: Path) -> None:
        """同卷原子发布：目标不存在直接 rename；遗留残目录先挪走再清理"""
        leaf_abs.parent.mkdir(parents=True, exist_ok=True)
        if leaf_abs.exists():
            old = stage / 'old'
            os.rename(leaf_abs, old)
            try:
                os.rename(stage_leaf, leaf_abs)
            except OSError:
                os.rename(old, leaf_abs)   # 回滚
                raise
            _assert_no_reparse(old)
            shutil.rmtree(old, ignore_errors=True)
        else:
            os.rename(stage_leaf, leaf_abs)

    # ---------- 删除 ----------

    def delete_model(self, key: str, ip: str = '') -> Dict[str, Any]:
        spec = self._catalog['sets'].get(key)
        if spec is None:
            raise ModelOpError(f'未知的模型标识: {key}')
        owner = f'model-delete-{key}'
        if not OPS_GATE.acquire(owner):
            return {'ok': False, 'error': 'conflict',
                    'detail': f'其他操作正在进行中（{OPS_GATE.owner}），稍后再试'}
        try:
            protected = self.protected_model_types()
            if spec['model_type'] in protected:
                return {'ok': False, 'error': 'not_allowed',
                        'detail': '该模型正在运行（或运行状态无法核实），禁止删除；'
                                  '请先安全停止，或切换到其他已安装模型'}
            leaves = []
            seen = set()
            for component in spec['components']:
                leaf = self._leaf_abs(component)
                self._validate_leaf_target(leaf)
                if str(leaf) not in seen:
                    seen.add(str(leaf))
                    leaves.append(leaf)
            if not any(leaf.exists() for leaf in leaves):
                return {'ok': False, 'error': 'not_allowed', 'detail': '该模型尚未安装'}
            selected_note = ''
            if self._selected_model_type() == spec['model_type']:
                selected_note = '（注意：它是当前设置选中的模型，删除后启动识别会失败）'
            deleted = []
            for leaf in leaves:
                if leaf.exists():
                    _assert_no_reparse(leaf)
                    shutil.rmtree(leaf)
                    deleted.append(str(leaf.relative_to(self._root())))
            # 清理因此变空的中间父目录（最多到 models 根为止）
            self._prune_empty_parents(leaves)
            audit.record('model.delete', ip,
                         detail=f'key={key} dirs={",".join(deleted)}{selected_note}')
            return {'ok': True, 'deleted': deleted, 'note': selected_note}
        finally:
            OPS_GATE.release(owner)

    def _prune_empty_parents(self, leaves: List[Path]) -> None:
        root_real = _norm(self._root())
        for leaf in leaves:
            parent = leaf.parent
            while True:
                parent_real = _norm(parent)
                if not parent_real.startswith(root_real + os.sep) or parent_real == root_real:
                    break
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent

    # ---------- 与其他模块的联动 ----------

    def set_complete(self, model_type: str) -> bool:
        """主组件必需文件是否齐全（切换/启动的门；主模型就绪即可切换）"""
        return self.main_ready(model_type)

    def main_ready(self, model_type: str) -> bool:
        """主组件（识别主模型）必需文件齐全"""
        for spec in self._catalog['sets'].values():
            if spec['model_type'] != model_type:
                continue
            main = next((c for c in spec['components'] if c.get('kind') == 'main'),
                        spec['components'][0])
            leaf = self._leaf_abs(main)
            if not leaf.is_dir():
                return False
            if any(not _file_present(leaf, rel) for rel in main['required']):
                return False
            return True
        return False

    def set_fully_complete(self, model_type: str) -> bool:
        """整套完整 = 全部组件（含必需的标点/对齐辅助）都已安装"""
        for spec in self._catalog['sets'].values():
            if spec['model_type'] != model_type:
                continue
            return all(self._component_installed(c) for c in spec['components'])
        return False

    def check_switchable(self, model_type: str) -> Tuple[bool, str]:
        if not self.set_complete(model_type):
            label = model_type
            for spec in self._catalog['sets'].values():
                if spec['model_type'] == model_type:
                    label = spec['label']
                    break
            return False, f'模型 {label} 未安装或不完整，只能切换到完整安装的模型（可在「语音模型管理」中下载）'
        return True, ''

    def check_start_allowed(self) -> Tuple[bool, str]:
        with self._lock:
            task = self._task
            if task and task.get('state') in ('pending', 'running'):
                return False, '模型下载任务进行中，请等待完成或取消后再启动识别进程'
        model_type = self._selected_model_type()
        ok, why = self.check_switchable(model_type)
        if not ok:
            return False, why + '；或修改服务设置中的识别模型'
        return True, ''
