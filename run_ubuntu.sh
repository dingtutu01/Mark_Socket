
### 说明：
# 1. 通过创建`.first_run_done`标记文件来区分首次运行和后续运行
# 2. 首次运行时执行`run_ubuntu.sh`的完整流程（包括系统依赖安装、Python环境配置、依赖包安装等），运行完成后创建标记文件
# 3. 后续运行时（检测到标记文件存在）执行`run_ubuntu.sh`的部分流程（仅激活现有环境并启动服务）
# 4. 保留了两个脚本中原有的错误检查和执行逻辑，确保功能一致性
# 5. 脚本执行逻辑：
#    - 第一次运行：会进行完整的环境搭建和服务启动
#    - 第二次及以后运行：直接使用已搭建好的环境启动服务{insert\_element\_0\_}
#!/bin/bash
set -e

# 定义第一次运行的标记文件
FIRST_RUN_MARKER=".first_run_done"

# 检查是否是第一次运行
if [ ! -f "$FIRST_RUN_MARKER" ]; then
    # 第一次运行：执行run_ubuntu.sh内容
    # 定义版本变量
    PYTHON_VERSION="3.8"
    FASTAPI_VERSION="0.116.1"
    UVICORN_VERSION="0.33.0"
    HTTPX_VERSION="0.28.1"
    PYDANTIC_VERSION="2.10.6"
    PILLOW_VERSION="10.4.0"
    REQUESTS_VERSION="2.31.0"

    # 1. 安装系统依赖及Python 3.8.20
    echo "=== 安装系统依赖及Python $PYTHON_VERSION ==="
    sudo apt-get update -y
    sudo apt-get install -y software-properties-common
    # 添加deadsnakes源（提供特定版本Python）
    sudo add-apt-repository -y ppa:deadsnakes/ppa
    sudo apt-get update -y
    # 安装Python 3.8及相关工具
    sudo apt-get install -y \
        python$PYTHON_VERSION \
        python$PYTHON_VERSION-venv \
        python$PYTHON_VERSION-dev \
        python3-pip

    # 2. 创建并激活Python 3.8虚拟环境
    echo "=== 创建Python $PYTHON_VERSION虚拟环境 ==="
    if [ ! -d "venv" ]; then
        python$PYTHON_VERSION -m venv venv
    fi
    source venv/bin/activate

    # 3. 升级pip并安装指定版本依赖
    echo "=== 安装指定版本依赖包 ==="
    pip install --upgrade pip==23.3.1  # 适配Python 3.8的最后一个pip版本
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

    # 5. 启动服务
    echo "=== 启动服务（Python $PYTHON_VERSION） ==="
    uvicorn main:app --host 0.0.0.0 --port 8001

    # 创建第一次运行完成的标记文件
    touch "$FIRST_RUN_MARKER"
else
    # 非第一次运行：执行run_ubun.sh内容
    # 1. 激活Python 3.8虚拟环境
    if [ ! -d "venv" ]; then
        python$PYTHON_VERSION -m venv venv
    fi
    source venv/bin/activate

    # 2. 检查main.py是否存在
    if [ ! -f "main.py" ]; then
        echo "错误：未找到main.py，请将脚本放在项目根目录执行"
        exit 1
    fi

    # 3. 启动服务
    echo "=== 启动服务（Python $PYTHON_VERSION） ==="
    uvicorn main:app --host 0.0.0.0 --port 8001
fi
