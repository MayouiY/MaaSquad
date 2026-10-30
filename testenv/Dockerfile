# MaaSquad 本地测试环境镜像：预装 Linux 原生环境与 Wine（Windows）环境，仓库在运行时挂载到 /workspace。
#
#   docker build -t maasquad-testenv -f testenv/Dockerfile .
#   docker run --rm -v "$PWD:/workspace" maasquad-testenv all
#   docker run --rm -it --network host -v "$PWD:/workspace" maasquad-testenv wine-serve --vnc
#
# 详见 docs/zh_cn/develop/test_env.md
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    TESTENV_HOME=/opt/maasquad-testenv \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-pip libatomic1 git ca-certificates curl unzip \
        wine wine64 xvfb xauth x11-utils x11vnc novnc websockify \
    && rm -rf /var/lib/apt/lists/* \
    # 挂载进来的仓库属于宿主机用户，允许 git 操作（拉取 OCR 子模块）
    && git config --system --add safe.directory '*'

WORKDIR /workspace
COPY testenv/ testenv/
COPY service/requirements.txt service/
COPY tools/requirements.txt tools/

# OCR 模型在仓库里，运行时由 setup / wine-setup 按需配置
RUN testenv/testenv.sh setup --no-ocr \
    && testenv/testenv.sh wine-setup --no-ocr \
    && rm -rf "$TESTENV_HOME/cache" /workspace/*

ENTRYPOINT ["/workspace/testenv/testenv.sh"]
CMD ["help"]
