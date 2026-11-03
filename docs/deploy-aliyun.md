# 部署到阿里云（含 Xiaomi-OCR-0）

目标：在阿里云 ECS 上跑起文档解析 MVP（`docs/ocr-mvp.md`），包括自己部署的 Xiaomi-OCR-0，供试点测试。全程三条命令，脚本都在 `deploy/aliyun/`。

## 1. 选哪种形态

| 形态 | 机器 | OCR | 适合 |
|---|---|---|---|
| **A. 一台 GPU 机全包（推荐先用这个测）** | GPU ECS，NVIDIA A10 24 GB，如 `ecs.gn7i-c8g1.2xlarge`（8 vCPU / 30 GiB） | 本机 vLLM 跑 Xiaomi-OCR-0 | 测试 OCR 效果、演示；按量付费，用完停机 |
| B. 普通机 + 百炼 | 普通 ECS，4 vCPU / 16 GiB 即可 | 百炼 Qwen-VL-OCR（按调用计费） | 还没有 GPU 预算时先跑通流程 |
| C. 应用机 + OCR 机分开（正式） | B 的应用机 + 一台只跑 OCR 的 GPU 机（同一 VPC 内网） | 另一台机上的 Xiaomi-OCR-0 | 正式使用；OCR 机可以按需开关，或换成 PAI-EAS |

说明：

- Xiaomi-OCR-0 只有 0.8B 参数，BF16 权重约 2 GB，一块 A10 绰绰有余（脚本默认只让它占 60% 显存）。T4（gn6i）更便宜，但不支持 BF16，没有验证过，不建议。
- 地域选离工厂近的华东（杭州 / 上海）。系统盘建议 ≥ 100 GB ESSD（vLLM 镜像约 10 GB 以上，加上模型和原件）。
- 价格以控制台为准；测试期用**按量付费**或**抢占式实例**，不测的时候停机。

## 2. 创建实例（控制台操作）

1. 云服务器 ECS → 创建实例：选上面的规格，镜像选 **Ubuntu 22.04 64 位**。GPU 实例**勾选"安装 GPU 驱动"**（选 CUDA 12.x 对应的驱动），这样开机就有 `nvidia-smi`。
2. 网络：分配公网 IP（或绑定 EIP）。
3. **安全组**（重要）：
   - 22（SSH）：只放行你自己的 IP；
   - 8000（网页与接口）：只放行办公网 / 你自己的 IP；
   - **不要**放行 5432（数据库）和 8001（OCR），它们只在机器内部用。
4. 登录后确认 GPU：`nvidia-smi` 能看到 A10。

## 3. 部署（三条命令）

```bash
# 取代码（GitHub 访问慢时，可以在本机 git archive 打包后 scp 上去）
git clone https://github.com/jayq0720/factory-ai-native-os.git && cd factory-ai-native-os

# ① 装 Docker（阿里云源）和 GPU 容器运行时
sudo DOCKER_MIRROR=https://<你的专属加速地址>.mirror.aliyuncs.com bash deploy/aliyun/setup-host.sh

# ② 从 ModelScope（魔搭，国内直连）下载 Xiaomi-OCR-0 权重到 /data/models/Xiaomi-OCR-0
sudo bash deploy/aliyun/download-model.sh

# ③ 构建并启动：数据库 + 接口 + Xiaomi-OCR-0，自动建表、做 OCR 自检、创建管理员并打印 API Key
sudo bash deploy/aliyun/deploy.sh gpu
```

最后会打印 `http://<公网 IP>:8000/app/` 和管理员 API Key（只显示一次）。浏览器打开、输入 Key 登录，把文件拖进去即可。

- 加速地址在"容器镜像服务 ACR → 镜像工具 → 镜像加速器"里，每个账号一个。如果加速器拉不到某个镜像（例如 `vllm/vllm-openai`），在能访问 Docker Hub 的机器上拉下来推到自己的 ACR 个人版仓库，再把 `deploy/.env` 里的 `VLLM_IMAGE` 改成那个地址。`PYTHON_IMAGE`、`PG_IMAGE` 同理。
- pip 默认走阿里云镜像（`PIP_INDEX_URL`）。

其他形态：

```bash
# B. 不用 GPU，用百炼：先在 deploy/.env 填 FOS_OCR_API_KEY=<百炼 API Key>
bash deploy/aliyun/deploy.sh bailian
# C. OCR 在另一台 GPU 机：那台机执行 ①② 后用 4.2 的方式只启动 OCR；应用机 deploy/.env 填
#    FOS_OCR_BASE_URL=http://<OCR 机内网 IP>:8001/v1（安全组只对应用机内网放行 8001）
bash deploy/aliyun/deploy.sh remote
# 不接 OCR，先看流程（Word / Excel / 文字版 PDF 仍是真实结果）
bash deploy/aliyun/deploy.sh mock
```

`deploy.sh` 可以重复执行：第一次会生成 `deploy/.env`（随机数据库密码，权限 600），之后只切换 OCR 模式、不覆盖你填过的值。升级：`git pull && sudo bash deploy/aliyun/deploy.sh gpu`。

## 4. Xiaomi-OCR-0 的部署细节

### 4.1 用 Docker（脚本默认）

`deploy/docker-compose.yml` 的 `ocr` 服务（`--profile gpu`）等价于：

```bash
vllm serve /models/Xiaomi-OCR-0 --served-model-name xiaomi-ocr-0 --port 8001 \
  --max-model-len 16384 --gpu-memory-utilization 0.6 --max-num-seqs 16
```

- 只读挂载本地权重，`HF_HUB_OFFLINE=1`，不连 Hugging Face。
- 模型架构是 `Qwen3_5ForConditionalGeneration`，需要支持它的 vLLM 版本。脚本默认用 `vllm/vllm-openai:latest`，**第一次跑通后把 `VLLM_IMAGE` 固定成当时的具体版本号**，避免以后升级出问题。
- 首次加载约 1–5 分钟，看进度：`docker compose -f deploy/docker-compose.yml --env-file deploy/.env logs -f ocr`。
- 我们这边按官方 demo 的整页解析提示词调用（关闭思考模式、`top_p=0.8`），表格输出的 OTSL 由我们转成 HTML 表格。

### 4.2 不用 Docker（OCR 单独一台机时更省事）

```bash
python3 -m venv /opt/vllm && /opt/vllm/bin/pip install -i https://mirrors.aliyun.com/pypi/simple/ vllm
/opt/vllm/bin/vllm serve /data/models/Xiaomi-OCR-0 --served-model-name xiaomi-ocr-0 \
  --host 0.0.0.0 --port 8001 --max-model-len 16384 --gpu-memory-utilization 0.8
```

长期运行可写成 systemd 服务（`ExecStart` 填上面那行，`Restart=always`）。SGLang 也可以：`python -m sglang.launch_server --model-path /data/models/Xiaomi-OCR-0 --served-model-name xiaomi-ocr-0 --port 8001 --context-length 16384`。

### 4.3 工厂本地（Mac Studio）

Ollama 有社区打包的 `longwayxu/xiaomi-ocr-0:bf16`（官方标注为实验性）：`ollama pull longwayxu/xiaomi-ocr-0:bf16`，然后 `FOS_OCR_BASE_URL=http://<Mac 内网 IP>:11434/v1`、`FOS_OCR_MODEL=longwayxu/xiaomi-ocr-0:bf16`，`deploy.sh remote`。效果要用样本和 GPU 版对比后再决定。

## 5. 验收

1. `docker compose -f deploy/docker-compose.yml --env-file deploy/.env exec api fos ocr-check`：发一张测试图，打印耗时和识别结果，应该能看到两行文字和一张 HTML 表格。也可以用自己的图：先 `docker cp 图.png fos-api-1:/tmp/a.png`，再 `fos ocr-check --image /tmp/a.png`。
2. 网页上传一份**扫描版** PDF（类别选"技术文件"），看每页解析方式显示"OCR"、没有"模拟数据"横幅，字段带页码，可以导出 Excel。
3. 记录 10 页左右扫描件的总耗时，按需调 `FOS_OCR_CONCURRENCY`（默认 4，A10 上可以试 8）。

## 6. 运维

| 事项 | 命令 / 做法 |
|---|---|
| 看状态与日志 | `docker compose -f deploy/docker-compose.yml --env-file deploy/.env ps`、`... logs -f api` |
| 给同事开账号 | `... exec api fos create-user luneng zhangsan 张三 --roles sales`，再 `... exec api fos issue-key luneng zhangsan` |
| 备份数据库 | `... exec -T db pg_dump -U fos fos \| gzip > fos-$(date +%F).sql.gz`；原件在 Docker 卷 `fos_blobs` |
| 停机省钱 | 控制台停止实例即可，数据在云盘上，开机后容器自动起来（`restart: unless-stopped`） |
| 换成 RDS | `deploy/.env` 加 `FOS_DATABASE_URL` 指向 RDS PostgreSQL，并去掉 compose 里的 `db` 服务 |

安全提醒：现在是 HTTP，API Key 明文传输，所以测试期一定要在安全组里限制来源 IP。正式使用前在前面加 HTTPS（SLB 或 Nginx + 证书）；用域名访问中国内地服务器需要先做 ICP 备案。

## 7. 常见问题

| 现象 | 处理 |
|---|---|
| `deploy.sh gpu` 提示没有 nvidia 运行时 | 先 `nvidia-smi` 确认驱动；再跑一次 `setup-host.sh` |
| 拉镜像卡住或 429 | 配镜像加速器，或把镜像推到自己的 ACR 后改 `*_IMAGE` |
| `ocr` 容器反复重启，日志里报不认识模型架构 | vLLM 版本太旧，换支持 Qwen3.5 的新版本镜像 |
| 显存不足（OOM） | 调小 `OCR_GPU_MEMORY_UTILIZATION`、`--max-num-seqs`，或把 `--max-model-len` 降到 8192 |
| 文档"失败：第 N 页 OCR 失败" | OCR 服务没起来或超时：看 `logs ocr`，好了以后在网页点"重新解析" |
| 下载模型很慢或中断 | 重跑 `download-model.sh`，支持断点续传 |
