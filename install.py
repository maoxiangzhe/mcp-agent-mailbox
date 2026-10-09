#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP 智能体邮箱 通用安装器
========================

把邮箱 MCP 服务器装进 4 种 AI 终端：

    claude     Claude Code   claude mcp add（幂等）
    codex      Codex CLI     ~/.codex/config.toml
    opencode   OpenCode      ~/.config/opencode/opencode.json
    trae       Trae          %APPDATA%\\Trae CN|Trae\\User\\mcp.json

两种服务器形态（``--module`` 选择）：

    1. 会话寻址邮箱（推荐，默认）：
           python -m mcp_agent_mailbox.cli serve
       一个邮箱账号对应一个原生会话；需要宿主注入会话身份环境变量
       （``MAILBOX_SESSION_ID`` / ``MAILBOX_HOST_TYPE`` / ``MAILBOX_HOST_INSTANCE_ID``）。

    2. 旧版公告板+收件箱（兼容期保留）：
           python server.py
       自由填写 ``agent`` 的旧接口，不安全，仅为不打断已有安装而保留。

用法：
    python install.py                  # 自动检测已安装的 CLI，逐个安装
    python install.py --target all     # 4 个终端全装（不检测，强制）
    python install.py --target codex   # 只装 Codex（可逗号分隔：claude,trae）
    python install.py --dry-run        # 演练：只检查环境，不实际写入
    python install.py --check          # 检查各终端安装状态（可按 --target 过滤）
    python install.py --project        # Claude 注册到当前项目级（默认用户级，仅影响 claude）
    python install.py --legacy         # 注册旧版 server.py（不推荐）
    python install.py --session-id ID  # 为该终端写死会话 ID（不推荐，见下）
    python install.py --capability 2   # 声明能力等级 0/1/2；只有验证过才写 2

关于会话身份（重要，务必读完）：
    邮箱账号 = 一个原生会话。所以 MCP 服务器必须知道"我是哪个会话"。
    适配器的正确做法是在启动 MCP 进程时注入 ``MAILBOX_SESSION_ID``：

        DSH   profile 里为该 MCP 服务器设置 env（DSH 默认会过滤 DSH_* 变量，
              因此只能显式配置；静态值无法区分会话，所以更推荐 in-process 插件路径）
        Codex  MCP 服务器配置支持 env；把目标会话 ID 写进对应工作区的配置
        其它  同理由客户端配置注入

    ``--session-id`` 会把一个固定会话 ID 写进客户端配置，只适合"这个客户端配置
    本来就只服务一个会话"的场景；多个会话共用会互相冒用身份，不要这么用。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

SERVER_NAME = CODEX_SERVER_NAME = 'mcp-agent-mailbox'
LEGACY_SERVER_NAME = 'board'
LEGACY_CODEX_SERVER_NAME = 'board-mcp'
# Codex Desktop/CLI 会把当前原生会话 ID 放在这两个变量之一。它们必须通过
# mcp_servers.<name>.env_vars 显式转发给 stdio 子进程；否则工具虽然能加载，
# whoami 仍会是 bound=false。不能把某个固定 ID 写进 MAILBOX_SESSION_ID，
# 那会让所有 Codex 会话冒用同一个邮箱账号。
CODEX_SESSION_ENV_VARS = ('CODEX_SESSION_ID', 'CODEX_THREAD_ID')
HERE = Path(__file__).resolve().parent
SERVER_PY = HERE / 'server.py'
LEGACY_TOOLS = ('get_board / claim_files / report_done / check_conflict / '
                'release_claim / post_decision / init_bulletin / send_note / read_notes / ack_notes')
NEW_TOOLS = ('whoami / connect_mailbox / list_contacts / start_conversation / send_message / reply_message / '
             'mailbox_inbox / list_conversations / read_conversation / set_message_status')
TARGETS = ('claude', 'codex', 'opencode', 'trae')
TARGET_LABELS = {
    'claude': 'Claude Code',
    'codex': 'Codex CLI',
    'opencode': 'OpenCode',
    'trae': 'Trae',
}
#: 各宿主在客户端配置里使用的宿主类型标识。
HOST_TYPE_BY_TARGET = {'claude': 'claude', 'codex': 'codex',
                       'opencode': 'opencode', 'trae': 'trae'}


class ServerSpec:
    '''"该注册哪个服务器、带哪些环境变量"的完整描述。'''

    def __init__(self, python_cmd: str, *, module_mode: bool,
                 session_id: str | None = None, capability: int | None = None,
                 data_dir: str | None = None) -> None:
        self.python_cmd = python_cmd
        self.module_mode = module_mode
        if module_mode:
            self.args = ['-m', 'mcp_agent_mailbox.cli', 'serve']
        else:
            self.args = [str(SERVER_PY)]
        self.session_id = session_id
        self.capability = capability
        self.data_dir = data_dir

    def env_for(self, target: str) -> dict[str, str]:
        '''该终端启动服务器时应注入的环境变量。

        会话身份必须由适配器注入；这里只在用户显式给出 --session-id 时才写入，
        因为把一个固定会话 ID 写进客户端配置会让多个会话互相冒用身份。
        '''
        if not self.module_mode:
            return {}
        env = {
            'MAILBOX_HOST_TYPE': HOST_TYPE_BY_TARGET.get(target, target),
            'MAILBOX_HOST_INSTANCE_ID': 'default',
        }
        if self.session_id:
            env['MAILBOX_SESSION_ID'] = self.session_id
        if self.capability is not None:
            env['MAILBOX_CAPABILITY_LEVEL'] = str(self.capability)
        if self.data_dir:
            env['MAILBOX_HOME'] = self.data_dir
        return env

    def server_name(self, target: str) -> str:
        if self.module_mode:
            return SERVER_NAME
        return LEGACY_CODEX_SERVER_NAME if target == 'codex' else LEGACY_SERVER_NAME

    def command_line(self) -> str:
        return ' '.join([self.python_cmd] + self.args)

    def tools_hint(self) -> str:
        return NEW_TOOLS if self.module_mode else LEGACY_TOOLS


def run(cmd: list[str], dry_run: bool) -> bool:
    '''执行命令（dry_run 时只打印不执行）。返回是否成功。'''
    print('  $', ' '.join(cmd))
    if dry_run:
        return True
    return subprocess.run(cmd).returncode == 0

def ensure_python() -> bool:
    '''检查 Python >= 3.10（server.py 的语法要求）。'''
    ok = sys.version_info >= (3, 10)
    print(f'[1] Python {sys.version.split()[0]} '
          f'({"满足要求 >=3.10" if ok else "不满足，请先安装 Python 3.10+"})')
    return ok

def install_deps(dry_run: bool) -> bool:
    '''检查 MCP SDK，以及 Python 3.10 安装配置时所需的 TOML 解析器。'''
    missing = []
    try:
        import mcp  # noqa: F401
    except ImportError:
        missing.append('mcp>=1.5,<2')
    if sys.version_info < (3, 11):
        try:
            import tomli  # noqa: F401
        except ImportError:
            missing.append('tomli>=2,<3')
    if not missing:
        print('[2] 依赖已安装，跳过')
        return True
    print('[2] 安装缺失依赖...')
    return run([sys.executable, '-m', 'pip', 'install'] + missing, dry_run)

def backup_file(path: Path) -> None:
    '''改动前备份到 %TEMP%/board_install_backup/，防止误改配置。'''
    if not path.exists():
        return
    bdir = Path(os.environ.get('TEMP', Path.home())) / 'board_install_backup'
    bdir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    name = str(path).replace(':', '_').replace('\\', '_').replace('/', '_')
    shutil.copy2(path, bdir / f'{name}.{stamp}.bak')

def _toml_literal(value: str) -> str:
    '''TOML 字符串：优先单引号字面量（Windows 路径反斜杠免转义）。'''
    if "'" not in value:
        return f"'{value}'"
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'

def _is_mailbox_entry(entry: dict) -> bool:
    args = entry.get('args', entry.get('command', []))
    if not isinstance(args, list):
        return False
    return any(args[i:i + 2] == ['-m', 'mcp_agent_mailbox.cli']
               for i in range(len(args) - 1))


def _read_toml(raw: str) -> dict:
    try:
        import tomllib
    except ImportError:  # Python 3.10 may have tomli installed separately.
        import tomli as tomllib
    return tomllib.loads(raw)


def _toml_value(value) -> str:
    if isinstance(value, str):
        return _toml_literal(value)
    if isinstance(value, list):
        return '[' + ', '.join(_toml_value(item) for item in value) + ']'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return str(value)


def _patch_toml_section(lines: list[str], section: str, values: dict) -> list[str]:
    """Replace only installer-owned keys; keep comments, extra keys and tables."""
    header = f'[{section}]'
    start = next((i for i, line in enumerate(lines)
                  if line.strip().split('#', 1)[0].strip() == header), None)
    if start is None:
        return lines + ([''] if lines else []) + [header] + [
            f'{key} = {_toml_value(value)}' for key, value in values.items()]
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].lstrip().startswith('[')), len(lines))
    remaining = dict(values)
    result = lines[:start + 1]
    i = start + 1
    while i < end:
        line = lines[i]
        match = re.match(r'\s*([\w-]+)\s*=', line)
        key = match.group(1) if match else None
        if key in remaining:
            result.append(f'{key} = {_toml_value(remaining.pop(key))}')
            # Lists may span lines. Parse the old value to find its end rather
            # than leaving continuation lines behind in the configuration.
            fragment = line
            while True:
                try:
                    _read_toml(fragment)
                    break
                except ValueError:
                    i += 1
                    if i >= end:
                        raise ValueError(f'无法解析 {section}.{key}')
                    fragment += '\n' + lines[i]
        else:
            result.append(line)
        i += 1
    result += [f'{key} = {_toml_value(value)}' for key, value in remaining.items()]
    return result + lines[end:]


def update_codex(spec: 'ServerSpec', dry_run: bool) -> bool:
    """Safely install mailbox; migrate only a known mailbox's legacy name."""
    cfg = Path.home() / '.codex' / 'config.toml'
    raw = cfg.read_text(encoding='utf-8') if cfg.exists() else ''
    try:
        servers = _read_toml(raw).get('mcp_servers', {})
    except (ValueError, ImportError) as exc:
        print(f'      无法解析 {cfg}（{exc}），保留原配置')
        return False
    name = spec.server_name('codex')
    existing = servers.get(name)
    if existing is not None and spec.module_mode and not _is_mailbox_entry(existing):
        print(f'      {name} 已被其他服务器占用，保留原配置')
        return False
    lines = raw.splitlines()
    # Rename the original sections in place, keeping disabled/tool permissions,
    # timeouts, custom environment and any nested tables intact.
    if spec.module_mode:
        old = servers.get(LEGACY_CODEX_SERVER_NAME)
        if old is not None and _is_mailbox_entry(old):
            old_prefix = f'mcp_servers.{LEGACY_CODEX_SERVER_NAME}'
            new_prefix = f'mcp_servers.{name}'
            if existing is None:
                existing = old
                lines = [re.sub(r'^(\s*\[)' + re.escape(old_prefix) + r'(?=[.\]])',
                                lambda match: match.group(1) + new_prefix, line)
                         for line in lines]
                print(f'      迁移邮箱名称 {LEGACY_CODEX_SERVER_NAME} -> {name}')
            else:
                # A partially migrated installation must not start two MCPs
                # for the same session. Only delete the confirmed mailbox.
                kept = []
                removing = False
                for line in lines:
                    if line.lstrip().startswith('['):
                        removing = bool(re.match(r'^\s*\[' + re.escape(old_prefix)
                                                 + r'(?=[.\]])', line))
                    if not removing:
                        kept.append(line)
                lines = kept
    existing = existing or {}
    args = list(spec.args)
    values = {'type': 'stdio', 'command': spec.python_cmd, 'args': args}
    env = dict(existing.get('env', {}))
    supplied = spec.env_for('codex')
    # Existing instance/data/session settings are user choices, not defaults
    # that a repeat installation may silently change.
    for key, value in supplied.items():
        env.setdefault(key, value)
    for key, provided in (('MAILBOX_SESSION_ID', spec.session_id),
                          ('MAILBOX_HOME', spec.data_dir),
                          ('MAILBOX_CAPABILITY_LEVEL', spec.capability)):
        if provided is not None:
            env[key] = str(provided)
    if spec.module_mode:
        old_args = existing.get('args', [])
        if 'serve' in old_args and _is_mailbox_entry(existing):
            args += [arg for arg in old_args[old_args.index('serve') + 1:]
                     if arg != '--allow-adapter-registration']
        args.append('--allow-adapter-registration')
        values['cwd'] = str(HERE)
        values['env_vars'] = list(dict.fromkeys(
            list(existing.get('env_vars', [])) + list(CODEX_SESSION_ENV_VARS)))
        if not spec.session_id and env.get('MAILBOX_SESSION_ID'):
            print('      保留已有固定 MAILBOX_SESSION_ID；多会话配置应由宿主逐会话注入身份。')
    try:
        new_lines = _patch_toml_section(lines, f'mcp_servers.{name}', values)
        if env:
            new_lines = _patch_toml_section(new_lines, f'mcp_servers.{name}.env', env)
        result = '\n'.join(new_lines) + '\n'
        _read_toml(result)  # Never write a malformed configuration.
    except ValueError as exc:
        print(f'      无法更新 {cfg}（{exc}），保留原配置')
        return False
    if result == raw:
        print(f'      {cfg} 已是最新，跳过')
        return True
    print(f'      更新 [mcp_servers.{name}] -> {cfg}')
    if not dry_run:
        backup_file(cfg)
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(result, encoding='utf-8')
    return True

def update_json(path: Path, dry_run: bool, mutate) -> bool:
    '''就地更新 JSON 配置（保留其他键）。mutate(dict) 修改数据。'''
    data = {}
    if path.exists():
        raw = path.read_text(encoding='utf-8')
        if raw.strip():
            try:
                data = json.loads(raw)
            except (OSError, json.JSONDecodeError) as exc:
                print(f'      读取失败 {path}（{exc}），跳过')
                return False
    before = json.dumps(data, ensure_ascii=False, indent=2)
    try:
        mutate(data)
    except (ValueError, TypeError) as exc:
        print(f'      无法更新 {path}（{exc}），保留原配置')
        return False
    after = json.dumps(data, ensure_ascii=False, indent=2)
    if before == after:
        print(f'      {path} 已是最新，跳过')
        return True
    print(f'      {"将更新" if dry_run else "更新"} {path}')
    if not dry_run:
        backup_file(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(after + '\n', encoding='utf-8')
    return True

def _claude_registration(claude: str, name: str = SERVER_NAME) -> tuple[bool, str, str]:
    '''查询 Claude 的指定注册信息，返回 (是否已注册, command, args)。'''
    check = subprocess.run([claude, 'mcp', 'get', name],
                           capture_output=True, text=True,
                           errors='replace')  # claude 输出含 √ 等非 GBK 字节
    if check.returncode != 0:
        return False, '', ''
    cmd = args = ''
    for line in check.stdout.splitlines():
        line = line.strip()
        if line.startswith('Command:'):
            cmd = line[len('Command:'):].strip().strip('"\'')
        elif line.startswith('Args:'):
            args = line[len('Args:'):].strip().strip('"\'')
    return True, cmd, args

def _same_path(a: str, b: str) -> bool:
    '''忽略大小写与斜杠方向比较路径。'''
    return a.replace('/', '\\').lower() == b.replace('/', '\\').lower()

def register_claude(spec: 'ServerSpec', scope: str, dry_run: bool) -> bool:
    '''注册进 Claude Code：已存在且指向一致则跳过，指向旧路径则重注册。'''
    claude = shutil.which('claude')
    if not claude:
        print('      未找到 claude 命令行，跳过（请先安装 Claude Code）')
        return False
    name = spec.server_name('claude')
    # Existing JSON configuration allows a safe rename without discarding
    # custom env/timeouts/permissions, unlike remove/add through the CLI.
    cfg = Path.cwd() / '.mcp.json' if scope == 'project' else Path.home() / '.claude.json'
    if cfg.exists():
        def mutate(data: dict) -> None:
            _merge_json_registration(data.setdefault('mcpServers', {}), spec, 'claude',
                                     {'command': spec.python_cmd, 'args': list(spec.args)}, 'env')
        return update_json(cfg, dry_run, mutate)
    if not dry_run:
        exists, cmd, args = _claude_registration(claude, name)
        if exists:
            if _same_path(cmd, spec.python_cmd) and args == ' '.join(spec.args):
                print(f'      MCP 服务器已存在且一致：{name}，跳过注册')
                return True
            print(f'      {name} 已注册且无法安全保留其设置，请在客户端配置中更新；保留原项')
            return False
    print(f'      注册 MCP 服务器（{scope} 级）...')
    env = spec.env_for('claude')
    if env:
        print(f'      提示：Claude Code 的 env 需在客户端配置里手动确认：{sorted(env)}')
    env_args = [part for key, value in env.items() for part in ('--env', f'{key}={value}')]
    ok = run([claude, 'mcp', 'add', name, '--scope', scope] + env_args + ['--',
              spec.python_cmd] + spec.args, dry_run)
    if ok:
        print(f'      MCP 服务器已注册：{name} -> {spec.command_line()}')
    return ok


def _merge_json_registration(servers: dict, spec: 'ServerSpec', target: str,
                             required: dict, env_key: str) -> None:
    name = spec.server_name(target)
    current = servers.get(name)
    if current is not None and spec.module_mode and not _is_mailbox_entry(current):
        raise ValueError(f'{name} 已被其他服务器占用')
    if spec.module_mode:
        old = servers.get(LEGACY_SERVER_NAME)
        if old is not None and _is_mailbox_entry(old):
            previous = servers.pop(LEGACY_SERVER_NAME)
            if current is None:
                current = previous
    entry = dict(current or {})
    entry.update(required)
    env = dict(entry.get(env_key, {}))
    for key, value in spec.env_for(target).items():
        env.setdefault(key, value)
    for key, provided in (('MAILBOX_SESSION_ID', spec.session_id),
                          ('MAILBOX_HOME', spec.data_dir),
                          ('MAILBOX_CAPABILITY_LEVEL', spec.capability)):
        if provided is not None:
            env[key] = str(provided)
    if env:
        entry[env_key] = env
    servers[name] = entry

def register_opencode(spec: 'ServerSpec', dry_run: bool) -> bool:
    '''注册进 OpenCode：~/.config/opencode/opencode.json 顶层 mcp 键。'''
    cfg = Path.home() / '.config' / 'opencode' / 'opencode.json'
    env = spec.env_for('opencode')

    def mutate(data: dict) -> None:
        entry = {
            'type': 'local',
            'command': [spec.python_cmd] + spec.args,
        }
        servers = data.setdefault('mcp', {})
        _merge_json_registration(servers, spec, 'opencode', entry, 'environment')
        servers[spec.server_name('opencode')].setdefault('enabled', True)

    print(f'      注册 MCP 服务器：{spec.server_name("opencode")} -> {cfg}')
    return update_json(cfg, dry_run, mutate)

def trae_json_paths() -> list[Path]:
    '''Trae 的 mcp.json 路径：国内版 Trae CN 与国际版 Trae。'''
    appdata = Path(os.environ.get('APPDATA', Path.home()))
    return [appdata / 'Trae CN' / 'User' / 'mcp.json',
            appdata / 'Trae' / 'User' / 'mcp.json']

def register_trae(spec: 'ServerSpec', dry_run: bool) -> bool:
    '''注册进 Trae：写入 mcpServers（Claude 风格）。'''
    paths = trae_json_paths()
    targets = [p for p in paths if p.exists() or p.parent.exists()]
    if not targets:
        targets = [paths[0]]
        print(f'      未检测到 Trae 目录，将写入默认路径 {paths[0]}（可在设置里改）')
    env = spec.env_for('trae')

    def mutate(data: dict) -> None:
        entry = {
            'command': spec.python_cmd,
            'args': list(spec.args),
        }
        _merge_json_registration(data.setdefault('mcpServers', {}), spec, 'trae', entry, 'env')

    ok = True
    for path in targets:
        print(f'      注册 MCP 服务器：{spec.server_name("trae")} -> {path}')
        ok &= update_json(path, dry_run, mutate)
    return ok

def register(target: str, spec: 'ServerSpec', scope: str, dry_run: bool) -> bool:
    '''按目标分派 MCP 注册。'''
    if target == 'claude':
        return register_claude(spec, scope, dry_run)
    if target == 'codex':
        return update_codex(spec, dry_run)
    if target == 'opencode':
        return register_opencode(spec, dry_run)
    return register_trae(spec, dry_run)

def detect_targets() -> list[str]:
    '''auto：检测本机已安装的 CLI。'''
    found = []
    if shutil.which('claude'):
        found.append('claude')
    if shutil.which('codex') or (Path.home() / '.codex' / 'config.toml').exists():
        found.append('codex')
    if shutil.which('opencode') or (Path.home() / '.config' / 'opencode').exists():
        found.append('opencode')
    if any(p.exists() or p.parent.exists() for p in trae_json_paths()):
        found.append('trae')
    return found

def resolve_targets(spec: str) -> list[str]:
    '''把 --target 参数解析成目标列表；未知值抛 ValueError。'''
    if spec == 'all':
        return list(TARGETS)
    if spec == 'auto':
        return detect_targets()
    targets = [t.strip().lower() for t in spec.split(',') if t.strip()]
    bad = [t for t in targets if t not in TARGETS]
    if bad:
        raise ValueError(f'未知目标：{", ".join(bad)}（可选：all / auto / {", ".join(TARGETS)}）')
    return targets

def check_claude(name: str = SERVER_NAME) -> tuple[bool, list[str]]:
    ok = True
    lines = []
    claude = shutil.which('claude')
    if claude:
        lines.append(f'  [claude]  cli: {claude}')
        r = subprocess.run([claude, 'mcp', 'get', name],
                           capture_output=True, text=True, errors='replace')
        reg = r.returncode == 0
        lines.append(f'  [claude]  mcp: {name} {"已注册" if reg else "未注册"}')
        ok &= reg
    else:
        lines.append('  [claude]  cli: 未检测到 claude，跳过')
    return ok, lines

def check_codex(name: str = CODEX_SERVER_NAME) -> tuple[bool, list[str]]:
    ok = True
    lines = []
    cfg = Path.home() / '.codex' / 'config.toml'
    if cfg.exists():
        try:
            has = name in _read_toml(cfg.read_text(encoding='utf-8')).get('mcp_servers', {})
        except (ValueError, ImportError):
            has = False
        lines.append(f'  [codex]  config: {cfg}  {name} {"已配置" if has else "未配置"}')
        ok &= has
    else:
        lines.append(f'  [codex]  config: 不存在 {cfg}')
        ok = False
    return ok, lines

def check_opencode(name: str = SERVER_NAME) -> tuple[bool, list[str]]:
    ok = True
    lines = []
    cfg = Path.home() / '.config' / 'opencode' / 'opencode.json'
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            data = {}
        has = name in data.get('mcp', {})
        lines.append(f'  [opencode]  config: {cfg}  mcp.{name} {"已配置" if has else "未配置"}')
        ok &= has
    else:
        lines.append(f'  [opencode]  config: 不存在 {cfg}')
        ok = False
    return ok, lines

def check_trae(name: str = SERVER_NAME) -> tuple[bool, list[str]]:
    ok = True
    lines = []
    found = False
    for path in trae_json_paths():
        if not path.exists():
            continue
        found = True
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            data = {}
        has = name in data.get('mcpServers', {})
        lines.append(f'  [trae]  {path}  mcpServers.{name} {"已配置" if has else "未配置"}')
        ok &= has
    if not found:
        lines.append('  [trae]  mcp.json 未检测到（Trae CN / Trae 都没装？）')
        ok = False
    return ok, lines

CHECKERS = {
    'claude': check_claude,
    'codex': check_codex,
    'opencode': check_opencode,
    'trae': check_trae,
}

def parse_args(argv: list[str]):
    '''解析命令行参数；非法参数返回 None（调用方以 exit 2 收尾）。'''
    from types import SimpleNamespace
    dry_run = check = project = legacy = False
    target = 'auto'
    module = 'mcp_agent_mailbox'
    session_id = data_dir = None
    capability = None
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg in ('-h', '--help'):
            print(__doc__)
            raise SystemExit(0)
        if arg == '--dry-run':
            dry_run = True
        elif arg == '--check':
            check = True
        elif arg == '--project':
            project = True
        elif arg == '--legacy':
            legacy = True
        elif arg in ('--module', '--session-id', '--capability', '--data-dir'):
            i += 1
            if i >= len(argv) or argv[i].startswith('--'):
                print(f'{arg} 需要值')
                return None
            value = argv[i]
            if arg == '--module':
                module = value
            elif arg == '--session-id':
                session_id = value
            elif arg == '--data-dir':
                data_dir = value
            else:
                if value not in ('0', '1', '2'):
                    print('--capability 只能是 0 / 1 / 2')
                    return None
                capability = int(value)
        elif arg == '--target':
            i += 1
            if i >= len(argv) or argv[i].startswith('--'):
                print('--target 需要值：all / auto / claude / codex / opencode / trae（可逗号分隔）')
                return None
            target = argv[i]
        elif arg.startswith('--target='):
            target = arg.split('=', 1)[1]
        else:
            print(f'未知参数：{arg}\n\n{__doc__}')
            return None
        i += 1
    if legacy and module != 'mcp_agent_mailbox':
        print('--legacy 与 --module 不能同时使用')
        return None
    return SimpleNamespace(
        dry_run=dry_run, check=check, project=project, legacy=legacy, module=module,
        session_id=session_id, capability=capability, data_dir=data_dir,
        scope='project' if project else 'user', target=target,
    )

def cmd_install(args) -> int:
    targets = resolve_targets(args.target)
    print('MCP 智能体邮箱 通用安装器')
    if args.dry_run:
        print('（演练模式：只检查环境，不实际写入）\n')
    ok = ensure_python()
    ok &= install_deps(args.dry_run)
    spec = ServerSpec(
        sys.executable,
        module_mode=not args.legacy,
        session_id=args.session_id,
        capability=args.capability,
        data_dir=args.data_dir,
    )
    if args.legacy:
        print('[!] 使用旧版服务器（server.py）：接口允许自由填写 agent，属不安全模式，'
              '仅为兼容期保留。新部署请去掉 --legacy。')
    else:
        print(f'[i] 会话寻址邮箱（{args.module}）：一个邮箱账号对应一个原生会话。')
        if not args.session_id:
            if 'codex' in targets:
                print('    Codex：安装器会开放 connect_mailbox。会话读取自己 Shell 中的 '
                      'CODEX_SESSION_ID 后显式注册，不会写死全局会话 ID。')
            if any(target != 'codex' for target in targets):
                print('    其他客户端：必须由宿主适配器注入会话 ID，'
                      '否则工具会如实报告"未绑定"。')
    if args.project and any(t != 'claude' for t in targets):
        print('（--project 仅影响 Claude 的注册范围，其余终端按用户级安装）')
    src = 'auto 自动检测' if args.target == 'auto' else f'--target {args.target}'
    print(f'目标终端：{"、".join(TARGET_LABELS[t] for t in targets)}（来源：{src}）')
    for target in targets:
        print(f'[{target}] {TARGET_LABELS[target]}')
        ok &= register(target, spec, args.scope, args.dry_run)
    print()
    if ok:
        print('[OK] 安装完成。重启对应终端会话后生效：')
        print(f'  工具：{spec.tools_hint()}')
    else:
        print('[FAIL] 安装未完成，请按上面提示处理。')
    return 0 if ok else 1

def cmd_check(args) -> int:
    targets = resolve_targets(args.target)
    print('MCP 智能体邮箱 安装状态检查')
    try:
        import mcp  # noqa: F401
        print(f'  [环境]  mcp SDK: 已安装（{sys.executable}）')
        mcp_ok = True
    except ImportError:
        print('  [环境]  mcp SDK: 未安装（运行 install.py 会自动安装）')
        mcp_ok = False
    ok = mcp_ok
    for target in targets:
        spec = ServerSpec(sys.executable, module_mode=not args.legacy)
        target_ok, lines = CHECKERS[target](spec.server_name(target))
        ok &= target_ok
        print('\n'.join(lines))
    print()
    print('[OK] 全部就绪' if ok else '[FAIL] 有组件缺失，运行 install.py 补装')
    return 0 if ok else 1

def main(argv: list[str]) -> int:
    # Windows 控制台常见 GBK 编码：先加固 stdout，避免特殊符号打印时崩溃
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass
    args = parse_args(argv)
    if args is None:
        return 2
    try:
        targets = resolve_targets(args.target)
    except ValueError as exc:
        print(exc)
        return 2
    if not targets:
        print('未检测到已安装的 AI 终端（claude / codex / opencode / trae）。')
        print('可用 --target all 强制安装到全部终端，或先安装对应 CLI。')
        return 1
    if args.check:
        return cmd_check(args)
    return cmd_install(args)

if __name__ == '__main__':
    sys.exit(main(sys.argv))
