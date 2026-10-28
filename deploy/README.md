# 部署模板

本目录提供 mppkb 的本地/Docker/systemd 部署模板。业务数据、PDF、环境变量和外部前端不包含在仓库中。

## Windows 本地

```powershell
# 1. 安装依赖
python -m venv .venv
.\.venv\Scripts\activate
pip install -r deploy\requirements.txt

# 2. 准备数据
#    deploy\data\kb.sqlite
#    deploy\data\corpus.sqlite
#    deploy\data\pdfs\

# 3. 克隆外部前端
git clone --depth 1 https://github.com/techki545/MPP_Project.git deploy\MPP_Project
python deploy\patch_frontend_graph.py --repo deploy\MPP_Project

# 4. 启动
.\deploy\run-bridge.ps1 -UIrepo .\deploy\MPP_Project -DataDir .\deploy\data -Port 8791
```

打开 http://127.0.0.1:8791/。

## Docker

```bash
git clone --depth 1 https://github.com/techki545/MPP_Project.git MPP_Project
cp .env.example .env
# 编辑 .env 中的数据路径与端口
docker compose up -d --build
curl -fsS http://127.0.0.1:8791/api/health
```

## Linux + systemd

参考 `mppkb-ui.service`、`nginx.conf.example` 和 `.env.example`；数据目录需要按实际路径配置。

## 配置

复制 `.env.example` 为 `.env` 后填写：

- `MPPKB_KB_DB` / `MPPKB_CORPUS_DB` / `MPPKB_CORPUS_DIR`
- `MPPKB_ONTOLOGY`
- `MPPKB_RETRIEVAL_MODE`（`lexical` 或 `hybrid`）
- 审核 SLA、答案回写、问题保留天数
- 混合检索/云端模型所需 API Key（不要提交 `.env`）

## 健康检查

```bash
curl -fsS http://127.0.0.1:8791/api/health
```
