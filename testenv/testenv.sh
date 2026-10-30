#!/usr/bin/env bash
# MaaSquad 本地测试环境：Linux 原生环境 + Wine（Windows）环境的统一入口。
# 详见 docs/zh_cn/develop/test_env.md，或执行 testenv/testenv.sh help。
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TESTENV_DIR="$REPO_DIR/testenv"
TESTENV_HOME="${TESTENV_HOME:-$REPO_DIR/.testenv}"
VENV="$TESTENV_HOME/venv"
CACHE="$TESTENV_HOME/cache"
LOGS="$TESTENV_HOME/logs"
PYTHON="${PYTHON:-python3}"

export WINEPREFIX="${TESTENV_WINEPREFIX:-$TESTENV_HOME/wine}"
WIN_PYTHON_VERSION="${WIN_PYTHON_VERSION:-3.12.10}"
NUGET_URL="${NUGET_URL:-https://api.nuget.org/v3-flatcontainer}"
WIN_PY_DIR="$WINEPREFIX/drive_c/python"
WIN_PY='C:\python\python.exe'
# 换成微软原版 VC++ 运行库的 DLL（由 msvc-runtime 安装到 C:\python）
NATIVE_DLLS=(concrt140 msvcp140 msvcp140_1 msvcp140_2 msvcp140_atomic_wait msvcp140_codecvt_ids vcomp140 vcruntime140 vcruntime140_1)

BG_PIDS=()
STARTED_XVFB=0

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m警告:\033[0m %s\n' "$*" >&2; }
die() {
    printf '\033[1;31m错误:\033[0m %s\n' "$*" >&2
    exit 1
}

have() { command -v "$1" >/dev/null 2>&1; }
need() { have "$1" || die "缺少命令 $1。$2"; }

as_root() {
    if [ "$(id -u)" = 0 ]; then
        "$@"
    elif have sudo; then
        sudo "$@"
    else
        die "需要 root 权限执行: $*"
    fi
}

cleanup() {
    local pid
    # 自己启动的 Xvfb 退出前先停掉 Wine，避免残留进程绑定在已关闭的显示上
    if [ "$STARTED_XVFB" = 1 ] && have wineserver; then
        wineserver -k 2>/dev/null || true
    fi
    for pid in "${BG_PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
    done
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# ---------------------------------------------------------------- 系统依赖

cmd_deps() {
    local wine=1 vnc=0 arg
    for arg in "$@"; do
        case "$arg" in
        --no-wine) wine=0 ;;
        --vnc) vnc=1 ;;
        *) die "deps 不支持参数 $arg" ;;
        esac
    done

    # libatomic1：Linux 版 maafw 依赖
    local pkgs=(python3 python3-venv python3-pip libatomic1 git ca-certificates curl unzip)
    [ "$wine" = 1 ] && pkgs+=(wine wine64 xvfb xauth x11-utils)
    [ "$vnc" = 1 ] && pkgs+=(x11vnc novnc websockify)

    if ! have apt-get; then
        warn "当前系统没有 apt-get，请用系统的包管理器手动安装: ${pkgs[*]}"
        return 1
    fi
    info "安装系统依赖: ${pkgs[*]}"
    as_root env DEBIAN_FRONTEND=noninteractive apt-get update
    as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${pkgs[@]}"
}

# ---------------------------------------------------------------- Linux 环境

ensure_venv() {
    [ -x "$VENV/bin/python" ] && return
    need "$PYTHON" "请先安装 Python 3.9+（testenv/testenv.sh deps）"
    "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 9))' || die "需要 Python 3.9 及以上: $("$PYTHON" --version)"
    info "创建虚拟环境 $VENV"
    mkdir -p "$TESTENV_HOME"
    "$PYTHON" -m venv "$VENV" || die "创建虚拟环境失败，Debian / Ubuntu 请先安装 python3-venv（testenv/testenv.sh deps）"
    "$VENV/bin/python" -m pip install -q --upgrade pip
}

maafw_pin() {
    if [ -n "${MAAFW_VERSION:-}" ]; then
        echo "maafw==$MAAFW_VERSION"
    fi
}

setup_ocr() {
    local model="$REPO_DIR/assets/resource/model/ocr"
    if [ -f "$model/rec.onnx" ]; then
        info "OCR 模型已就绪: assets/resource/model/ocr"
        return
    fi
    if [ ! -d "$REPO_DIR/assets/MaaCommonAssets/OCR" ]; then
        info "拉取 MaaCommonAssets 子模块（OCR 模型）"
        if ! git -C "$REPO_DIR" submodule update --init --depth 1 assets/MaaCommonAssets; then
            warn "子模块拉取失败，OCR 相关测试会被跳过。可参考 docs/zh_cn/develop/how_to_develop.md 手动放置模型"
            return
        fi
    fi
    "$VENV/bin/python" "$REPO_DIR/tools/configure.py"
}

cmd_setup() {
    local ocr=1 arg
    for arg in "$@"; do
        case "$arg" in
        --no-ocr) ocr=0 ;;
        *) die "setup 不支持参数 $arg" ;;
        esac
    done

    ensure_venv
    info "安装 Linux 端依赖（maafw、aiohttp 等）"
    # shellcheck disable=SC2046
    "$VENV/bin/pip" install -q --upgrade -r "$TESTENV_DIR/requirements.txt" $(maafw_pin)
    [ "$ocr" = 1 ] && setup_ocr
    local version
    version="$("$VENV/bin/python" -c 'from maa.library import Library; print(Library.version())')" ||
        die "maafw 无法加载，请检查上面的报错（缺少系统库时可执行 testenv/testenv.sh deps）"
    ok "Linux 环境就绪: MaaFramework $version"
}

ensure_setup() {
    if ! "$VENV/bin/python" -c 'import maa, aiohttp, jsonschema' 2>/dev/null; then
        cmd_setup
    fi
}

cmd_check() {
    local node=1 arg failed=()
    for arg in "$@"; do
        case "$arg" in
        --no-node) node=0 ;;
        *) die "check 不支持参数 $arg" ;;
        esac
    done
    ensure_setup
    cd "$REPO_DIR"

    info "JSON Schema 校验"
    "$VENV/bin/python" tools/validate_schema.py --schema-dir deps/tools --resource-dirs assets/resource \
        --exclude-dirs assets/resource/announcement --interface-files assets/interface.json || failed+=("schema")

    if [ "$node" = 1 ] && have npm; then
        info "maa-tools 资源检查"
        { npm ci --no-audit --no-fund --loglevel=error && npx @nekosu/maa-tools check; } || failed+=("maa-tools")
    elif [ "$node" = 1 ]; then
        warn "未安装 Node.js，跳过 maa-tools 资源检查"
    fi

    info "Web 服务单元测试（Linux）"
    "$VENV/bin/python" -m unittest discover -s service/tests -v || failed+=("unittest")

    if [ "${#failed[@]}" -gt 0 ]; then
        warn "未通过: ${failed[*]}"
        return 1
    fi
    ok "Linux 检查全部通过"
}

cmd_serve() {
    ensure_setup
    mkdir -p "$TESTENV_HOME/user"
    cd "$REPO_DIR"
    info "用户目录 .testenv/user，无设备时连接请求可传 {\"controller_type\": \"Debug\"}"
    exec "$VENV/bin/python" service/main.py --user-dir "$TESTENV_HOME/user" "$@"
}

# ---------------------------------------------------------------- Wine 环境

winpath() {
    local p
    p="$(realpath -m "$1")"
    printf 'Z:%s' "${p//\//\\}"
}

wine_env() {
    need wine "请先安装 Wine（testenv/testenv.sh deps）"
    export WINEARCH=win64 WINEDEBUG="${WINEDEBUG:--all}" PYTHONUTF8=1 PYTHONIOENCODING=utf-8 PYTHONUNBUFFERED=1
    unset PYTHONHOME PYTHONPATH VIRTUAL_ENV
}

# Wine 9 中，标准输出 / 错误是普通文件时 Windows 版 Python 无法初始化（WinError 6），非终端时统一经管道输出
wine_run() {
    if [ -t 1 ] && [ -t 2 ]; then
        wine "$@"
    else
        local rc=0
        wine "$@" 2>&1 | cat || rc=$?
        return "$rc"
    fi
}

wine_py() {
    wine_run "$WIN_PY" "$@"
}

start_display() {
    if [ "$STARTED_XVFB" = 1 ] || { [ -n "${DISPLAY:-}" ] && [ "${TESTENV_XVFB:-auto}" != 1 ]; }; then
        return
    fi
    need Xvfb "请先安装 xvfb（testenv/testenv.sh deps）"
    mkdir -p "$LOGS"
    # 由 Xvfb 自己挑选空闲的显示号，就绪后写入 displayfd
    local display_file="$LOGS/xvfb.display" n
    rm -f "$display_file"
    Xvfb -displayfd 3 -screen 0 "${XVFB_SCREEN:-1600x900x24}" -nolisten tcp 3>"$display_file" >"$LOGS/xvfb.log" 2>&1 &
    BG_PIDS+=($!)
    STARTED_XVFB=1
    for _ in $(seq 100); do
        if [ -s "$display_file" ]; then
            n="$(tr -dc 0-9 <"$display_file")"
            export DISPLAY=":$n"
            return
        fi
        kill -0 "${BG_PIDS[-1]}" 2>/dev/null || die "Xvfb 启动失败，见 $LOGS/xvfb.log"
        sleep 0.1
    done
    die "Xvfb 启动超时"
}

init_prefix() {
    if [ ! -f "$WINEPREFIX/system.reg" ]; then
        info "初始化 Wine 前缀 $WINEPREFIX"
        mkdir -p "$WINEPREFIX"
        # 不安装 Wine Mono / Gecko，测试用不到
        WINEDLLOVERRIDES="mscoree,mshtml=" wine wineboot --init >/dev/null 2>&1 || true
        wineserver -w
        [ -f "$WINEPREFIX/system.reg" ] || die "Wine 前缀初始化失败"
    fi

    local reg="$WINEPREFIX/maasquad-testenv.reg" dll
    {
        printf 'REGEDIT4\r\n\r\n[HKEY_CURRENT_USER\\Software\\Wine\\DllOverrides]\r\n'
        for dll in "${NATIVE_DLLS[@]}"; do
            printf '"%s"="native,builtin"\r\n' "$dll"
        done
        # 程序崩溃时不弹出对话框，避免无人值守的测试卡住
        printf '\r\n[HKEY_CURRENT_USER\\Software\\Wine\\WineDbg]\r\n"ShowCrashDialog"=dword:00000000\r\n'
    } >"$reg"
    wine regedit /S "$(winpath "$reg")" >/dev/null 2>&1
}

install_win_python() {
    if [ "$(cat "$WIN_PY_DIR/.version" 2>/dev/null)" = "$WIN_PYTHON_VERSION" ]; then
        return
    fi
    need curl "请先安装 curl"
    need unzip "请先安装 unzip"
    local pkg="$CACHE/python.$WIN_PYTHON_VERSION.nupkg" tmp
    mkdir -p "$CACHE"
    if [ ! -f "$pkg" ]; then
        info "下载 Windows 版 Python $WIN_PYTHON_VERSION（NuGet 官方包）"
        curl -fsSL --retry 3 -o "$pkg.part" "$NUGET_URL/python/$WIN_PYTHON_VERSION/python.$WIN_PYTHON_VERSION.nupkg"
        mv "$pkg.part" "$pkg"
    fi
    info "安装 Windows 版 Python 到 C:\\python"
    tmp="$(mktemp -d)"
    unzip -q "$pkg" 'tools/*' -d "$tmp"
    rm -rf "$WIN_PY_DIR"
    mv "$tmp/tools" "$WIN_PY_DIR"
    rm -rf "$tmp"
    echo "$WIN_PYTHON_VERSION" >"$WIN_PY_DIR/.version"
}

cmd_wine_setup() {
    local ocr=1 arg
    for arg in "$@"; do
        case "$arg" in
        --no-ocr) ocr=0 ;;
        *) die "wine-setup 不支持参数 $arg" ;;
        esac
    done
    wine_env
    ensure_venv
    start_display
    init_prefix
    install_win_python

    local pyver="${WIN_PYTHON_VERSION%.*}"
    local wheels="$CACHE/wheels-win-py$pyver"
    info "下载 Windows 依赖（win_amd64 wheel）"
    # shellcheck disable=SC2046
    "$VENV/bin/pip" download -q --only-binary=:all: --platform win_amd64 --python-version "$pyver" --implementation cp \
        -d "$wheels" -r "$TESTENV_DIR/wine/requirements.txt" $(maafw_pin)
    info "在 Wine 中安装依赖"
    # shellcheck disable=SC2046
    wine_py -m pip install -q --disable-pip-version-check --no-warn-script-location --upgrade --no-index \
        --find-links "$(winpath "$wheels")" -r "$(winpath "$TESTENV_DIR/wine/requirements.txt")" $(maafw_pin)

    [ "$ocr" = 1 ] && setup_ocr
    local version
    version="$(wine_py -c 'import platform; from maa.library import Library; print("MaaFramework", Library.version(), "/ Python", platform.python_version(), "/", platform.platform())')" ||
        die "Wine 中的 maafw 无法加载，请检查上面的报错"
    ok "Wine 环境就绪: ${version//$'\r'/}"
}

ensure_wine_setup() {
    wine_env
    if [ ! -f "$WIN_PY_DIR/python.exe" ] || ! wine_py -c 'import maa, aiohttp, PIL' >/dev/null 2>&1; then
        cmd_wine_setup
    fi
}

cmd_wine_test() {
    local suite="${1:-all}" failed=()
    case "$suite" in all | service | win32) ;; *) die "wine-test 的参数只能是 all / service / win32" ;; esac
    wine_env
    start_display
    ensure_wine_setup
    cd "$REPO_DIR"

    if [ "$suite" != win32 ]; then
        info "Web 服务单元测试（Wine / Windows 版 Python）"
        wine_py -m unittest discover -s service/tests -v || failed+=("service")
    fi
    if [ "$suite" != service ]; then
        info "Win32 端到端测试（Wine）"
        wine_py -m unittest discover -s testenv/wine -p 'test_*.py' -v || failed+=("win32")
    fi

    if [ "${#failed[@]}" -gt 0 ]; then
        warn "未通过: ${failed[*]}"
        return 1
    fi
    ok "Wine 测试全部通过"
}

start_vnc() {
    need x11vnc "请先安装 x11vnc（testenv/testenv.sh deps --vnc）"
    local vnc_port="${VNC_PORT:-5900}" web_port="${NOVNC_PORT:-6080}" novnc="${NOVNC_DIR:-/usr/share/novnc}"
    x11vnc -display "$DISPLAY" -localhost -forever -shared -nopw -quiet -rfbport "$vnc_port" >"$LOGS/x11vnc.log" 2>&1 &
    BG_PIDS+=($!)
    info "VNC: 127.0.0.1:$vnc_port（仅本机，无密码）"
    if have websockify && [ -d "$novnc" ]; then
        websockify --web "$novnc" "127.0.0.1:$web_port" "127.0.0.1:$vnc_port" >"$LOGS/novnc.log" 2>&1 &
        BG_PIDS+=($!)
        info "浏览器查看虚拟桌面: http://127.0.0.1:$web_port/vnc.html"
    fi
}

start_target() {
    mkdir -p "$LOGS"
    wine_py "$(winpath "$TESTENV_DIR/wine/target_app.py")" --log "$(winpath "$LOGS/target-events.jsonl")" >"$LOGS/target.log" 2>&1 &
    BG_PIDS+=($!)
    info "测试目标窗口已启动，输入事件记录在 .testenv/logs/target-events.jsonl"
}

cmd_wine_serve() {
    local vnc=0 target=1 args=()
    while [ $# -gt 0 ]; do
        case "$1" in
        --vnc) vnc=1 ;;
        --no-target) target=0 ;;
        *) args+=("$1") ;;
        esac
        shift
    done
    wine_env
    start_display
    ensure_wine_setup
    mkdir -p "$TESTENV_HOME/wine-user" "$LOGS"
    [ "$vnc" = 1 ] && start_vnc
    [ "$target" = 1 ] && start_target

    cd "$REPO_DIR"
    info "在 Wine 中启动 Web 服务（配置 testenv/wine/web_service.json，Ctrl+C 退出）"
    wine_py service/main.py --user-dir "$(winpath "$TESTENV_HOME/wine-user")" \
        --config "$(winpath "$TESTENV_DIR/wine/web_service.json")" "${args[@]}"
}

cmd_wine_target() {
    local vnc=0 arg
    for arg in "$@"; do
        case "$arg" in
        --vnc) vnc=1 ;;
        *) die "wine-target 不支持参数 $arg" ;;
        esac
    done
    wine_env
    start_display
    ensure_wine_setup
    [ "$vnc" = 1 ] && start_vnc
    info "测试目标窗口运行中（DISPLAY=$DISPLAY），Ctrl+C 退出"
    wine_py "$(winpath "$TESTENV_DIR/wine/target_app.py")" --log "$(winpath "$LOGS/target-events.jsonl")"
}

cmd_wine_python() {
    wine_env
    start_display
    ensure_wine_setup
    wine_py "$@"
}

# ---------------------------------------------------------------- 其它

cmd_status() {
    echo "仓库目录      : $REPO_DIR"
    echo "环境目录      : $TESTENV_HOME"
    if [ -x "$VENV/bin/python" ]; then
        echo "Linux Python  : $("$VENV/bin/python" --version 2>&1)"
        echo "Linux maafw   : $("$VENV/bin/python" -c 'from maa.library import Library; print(Library.version())' 2>/dev/null || echo 未安装)"
    else
        echo "Linux 环境    : 未创建（testenv/testenv.sh setup）"
    fi
    if [ -f "$REPO_DIR/assets/resource/model/ocr/rec.onnx" ]; then
        echo "OCR 模型      : 已配置"
    else
        echo "OCR 模型      : 未配置"
    fi
    if have wine; then
        echo "Wine          : $(wine --version 2>/dev/null)"
        if [ -f "$WIN_PY_DIR/python.exe" ]; then
            wine_env
            echo "Wine 前缀     : $WINEPREFIX"
            echo "Windows Python: $(cat "$WIN_PY_DIR/.version")"
            echo "Windows maafw : $(wine_py -c 'from maa.library import Library; print(Library.version())' 2>/dev/null | tr -d '\r' || echo 未安装)"
        else
            echo "Wine 环境     : 未创建（testenv/testenv.sh wine-setup）"
        fi
    else
        echo "Wine          : 未安装（testenv/testenv.sh deps）"
    fi
}

cmd_clean() {
    local all=0 arg
    for arg in "$@"; do
        case "$arg" in
        --all) all=1 ;;
        *) die "clean 不支持参数 $arg" ;;
        esac
    done
    if have wineserver && [ -d "$WINEPREFIX" ]; then
        WINEPREFIX="$WINEPREFIX" wineserver -k 2>/dev/null || true
    fi
    if [ "$all" = 1 ]; then
        info "删除 $TESTENV_HOME"
        rm -rf "$TESTENV_HOME"
    else
        info "删除虚拟环境与 Wine 前缀（保留下载缓存 $CACHE）"
        rm -rf "$VENV" "$WINEPREFIX" "$TESTENV_HOME/user" "$TESTENV_HOME/wine-user" "$LOGS"
    fi
}

cmd_all() {
    # 每一步在独立的进程里执行，保证 set -e 生效（if 条件里的函数调用会忽略 set -e）
    local self="$TESTENV_DIR/testenv.sh" results=() rc=0 r
    if bash "$self" setup && bash "$self" check "$@"; then results+=("Linux: 通过"); else results+=("Linux: 失败"); rc=1; fi
    if have wine; then
        if bash "$self" wine-test; then results+=("Wine: 通过"); else results+=("Wine: 失败"); rc=1; fi
    else
        results+=("Wine: 跳过（未安装 wine，可执行 testenv/testenv.sh deps）")
    fi
    printf '\n'
    for r in "${results[@]}"; do info "$r"; done
    return "$rc"
}

usage() {
    cat <<'EOF'
MaaSquad 本地测试环境

用法: testenv/testenv.sh <命令> [参数]

准备
  deps [--no-wine] [--vnc]   用 apt 安装系统依赖（Python、Wine、Xvfb；--vnc 额外安装 x11vnc / noVNC）

Linux 原生环境
  setup [--no-ocr]           创建 .testenv/venv，安装 maafw 与服务依赖，配置 OCR 模型
  check [--no-node]          JSON Schema 校验 + maa-tools 检查 + Web 服务单元测试（同 CI）
  serve [服务参数...]         在 Linux 上启动 Web 服务，参数透传给 service/main.py

Wine（Windows）环境
  wine-setup [--no-ocr]      创建 Wine 前缀，安装 Windows 版 Python、maafw 与服务依赖
  wine-test [all|service|win32]
                             在 Wine 中运行 Web 服务单元测试与 Win32 端到端测试
  wine-serve [--vnc] [--no-target] [服务参数...]
                             在虚拟桌面中启动测试目标窗口和 Windows 版 Web 服务
  wine-target [--vnc]        只启动测试目标窗口
  wine-python [参数...]       用 Wine 中的 Windows 版 Python 执行命令

其它
  all [--no-node]            setup + check + wine-test，最后汇总结果
  status                     查看环境状态
  clean [--all]              删除虚拟环境与 Wine 前缀（--all 连同下载缓存）

常用环境变量
  TESTENV_HOME        环境目录，默认 <仓库>/.testenv
  MAAFW_VERSION       固定 maafw 版本（Linux 与 Windows 同时生效）
  WIN_PYTHON_VERSION  Wine 中的 Python 版本，默认 3.12.10
  NUGET_URL           Windows 版 Python 的 NuGet 源，默认 https://api.nuget.org/v3-flatcontainer
  TESTENV_XVFB=1      即使已有 DISPLAY 也使用独立的 Xvfb 虚拟显示
EOF
}

main() {
    local cmd="${1:-help}"
    [ $# -gt 0 ] && shift
    case "$cmd" in
    deps) cmd_deps "$@" ;;
    setup) cmd_setup "$@" ;;
    check) cmd_check "$@" ;;
    serve) cmd_serve "$@" ;;
    wine-setup) cmd_wine_setup "$@" ;;
    wine-test) cmd_wine_test "$@" ;;
    wine-serve) cmd_wine_serve "$@" ;;
    wine-target) cmd_wine_target "$@" ;;
    wine-python) cmd_wine_python "$@" ;;
    all) cmd_all "$@" ;;
    status) cmd_status "$@" ;;
    clean) cmd_clean "$@" ;;
    help | -h | --help) usage ;;
    *)
        usage >&2
        exit 2
        ;;
    esac
}

main "$@"
