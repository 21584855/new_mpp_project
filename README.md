# MPP Knowledge Governance (mppkb)

一个面向临床证据治理的 **claim 级知识库 + 检索编排 + 审核审计** 工程原型。

- 冷/热双库：热库候选经人工审核后晋升冷库
- append-only `claim_events`，冷库为物化视图，支持重建校验
- claim slot：`population | severity | intervention | attribute`（AE 可选 `arm`）
- 证据状态：版本、快照、热度、冲突、缺口、逐句引用核查
- 审核台：队列、批量/claim 级裁决、SLA、台账、影响集
- 问答：确定性结论、治疗要素、来源全文详情、证据缺口自动补检索

> 本项目是研究/工程原型，不构成临床建议；所有知识内容需经临床负责人审核。

## 仓库不包含

为避免上传敏感或内部材料，本仓库**不包含**：

- 数据库文件（`kb.sqlite`、`corpus.sqlite`）
- PDF 原文、语料、导入数据
- 审核记录、答案日志
- 内部总结、工单详情、评测报告
- 前端 `MPP_Project` 源码（作为外部依赖）

运行前需要自行准备受治理的证据数据和前端仓库。

## 环境

```bash
python -m venv .venv
# Windows
.\.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate

pip install -r deploy/requirements.txt
# 需要混合检索时：
pip install -r deploy/requirements-hybrid.txt
```

要求 Python 3.11+。

## 数据目录

默认使用 `deploy/data/`：

```text
deploy/data/
  kb.sqlite
  corpus.sqlite
  pdfs/
```

也可以通过环境变量指定：

```bash
set MPPKB_KB_DB=...\kb.sqlite
set MPPKB_CORPUS_DB=...\corpus.sqlite
set MPPKB_CORPUS_DIR=...\pdfs
set MPPKB_ONTOLOGY=...\ontology.yaml
set MPPKB_RETRIEVAL_MODE=lexical
```

## CLI 快速验证

```bash
python -m mppkb status --json
python -m mppkb answer --query "儿童 阿奇霉素 用法" --json
python -m mppkb rebuild --verify
```

## 启动审核/问答页面

页面需要外部 `MPP_Project` 前端（不包含在本仓库）：

```bash
git clone --depth 1 https://github.com/techki545/MPP_Project.git deploy/MPP_Project
python deploy/patch_frontend_graph.py --repo deploy/MPP_Project
```

Windows：

```powershell
.\deploy\run-bridge.ps1 -UIrepo .\deploy\MPP_Project -DataDir .\deploy\data -Port 8791
```

打开 http://127.0.0.1:8791/。

## 测试

```bash
python -m pytest -q
```

测试使用 `tests/fixtures` 中的合成 PDF，不会读取业务数据。

## 主要模块

| 模块 | 作用 |
|---|---|
| `mppkb/answer.py` | 检索、证据状态、结论、治疗要素、引用核查 |
| `mppkb/gapfill.py` | 缺口补检索（只读：热库候选/原文线索） |
| `mppkb/index.py` | 词法/混合检索与重排 |
| `mppkb/promote.py`、`mppkb/conflict.py` | 审核、冲突与晋升 |
| `mppkb/rebuild.py` | 事件重放与重建校验 |
| `mppkb/graph.py` | 复杂病例编排与 Human Gate |
| `mppkb/ui_bridge.py` | 审核台/问答 API 与页面注入 |
| `deploy/` | 本地/Docker/systemd 部署模板 |

## 数据安全

- 不要提交 `.env`、数据库、PDF、审核日志。
- 生产使用前请补充权限、加密、脱敏和临床 UAT。
- 建议在上传公开仓库前确认数据许可和内容合规。
