#!/bin/bash
set -e

# 定义首次运行标记文件
FIRST_RUN_MARKER=".first_run_completed"

# 检查是否为首次运行
if [ ! -f "$FIRST_RUN_MARKER" ]; then
    # 首次运行：执行run_centos.sh的内容
    echo "=== 首次运行，开始环境配置 ==="

    # 定义版本变量
    PYTHON_VERSION="3.8"
    FASTAPI_VERSION="0.116.1"
    UVICORN_VERSION="0.33.0"
    HTTPX_VERSION="0.28.1"
    PYDANTIC_VERSION="2.10.6"
    PILLOW_VERSION="10.4.0"
    REQUESTS_VERSION="2.31.0"

    # 1. 安装系统依赖及Python 3.8
    echo "=== 安装系统依赖及Python $PYTHON_VERSION ==="
    sudo yum install -y epel-release
    sudo yum install -y https://repo.ius.io/ius-release-el7.rpm  # 若为CentOS 8，替换为：https://repo.ius.io/ius-release-el8.rpm
    sudo yum makecache fast
    sudo yum install -y \
        python38 \
        python38-venv \
        python38-devel \
        python3-pip

    # 2. 创建并激活Python虚拟环境
    echo "=== 创建Python $PYTHON_VERSION虚拟环境 ==="
    if [ ! -d "venv" ]; then
        python3.8 -m venv venv
    fi
    source venv/bin/activate

    # 3. 升级pip并安装指定版本依赖
    echo "=== 安装指定版本依赖包 ==="
    pip install --upgrade pip==23.3.1
    pip install \
        fastapi==$FASTAPI_VERSION \
        uvicorn==$UVICORN_VERSION \
        httpx==$HTTPX_VERSION \
        pydantic==$PYDANTIC_VERSION \
        pillow==$PILLOW_VERSION \
        requests==$REQUESTS_VERSION

    # 4. 检查main.py是否存在
    if [ ! -f "main.py" ]; then
        echo "错误：未找到main.py，请将脚本放在项目根目录执行"
        exit 1
    fi

    # 创建首次运行完成标记
    touch "$FIRST_RUN_MARKER"
    echo "=== 首次环境配置完成 ==="
else
    # 非首次运行：执行run_cent.sh的内容
    echo "=== 非首次运行，直接启动服务 ==="

    # 1. 激活Python虚拟环境
    if [ ! -d "venv" ]; then
        echo "错误：未找到虚拟环境venv，请检查环境是否正确配置"
        exit 1
    fi
    source venv/bin/activate

    # 2. 检查main.py是否存在
    if [ ! -f "main.py" ]; then
        echo "错误：未找到main.py，请将脚本放在项目根目录执行"
        exit 1
    fi
fi

# 启动服务（两种模式共用的启动步骤）
echo "=== 启动服务（Python $PYTHON_VERSION） ==="
uvicorn main:app --host 0.0.0.0 --port 8001
