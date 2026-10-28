# 合成演示数据（测试版）

> **SYNTHETIC DEMO DATA — NOT CLINICAL — 不可用于临床决策。**
>
> 这里的 PDF、claim、数据库全部是合成/测试内容，只用于验证系统流程。
> 不要与本机正式库共用数据目录，也不要把 `demo/data` 提交到 Git。

## 一键生成

在仓库根目录：

```powershell
python tools/seed_demo.py
```

脚本会：

1. 只读取 `demo/fixtures/demo_mini.pdf`（合成测试 PDF）
2. 在 `demo/data/` 创建独立的 `kb.sqlite`、`corpus.sqlite`
3. 写入 7 条合成 claim 并自动归一/晋升
4. 跑一次演示回答，输出状态/缺口/补检索结果

不会修改正式库，也不会读取业务数据。

## 演示查询

```powershell
$env:MPPKB_KB_DB="$PWD\demo\data\kb.sqlite"
$env:MPPKB_CORPUS_DB="$PWD\demo\data\corpus.sqlite"
$env:MPPKB_CORPUS_DIR="$PWD\demo\data\pdfs"
$env:MPPKB_ONTOLOGY="$PWD\ontology.yaml"
$env:MPPKB_ORCHESTRATION_V2="1"

python -m mppkb answer --query "儿童 阿奇霉素 剂量 疗程 禁忌" --json
```

## 启动测试版页面

需要外部 `MPP_Project` 前端（不在本仓库）：

```powershell
git clone --depth 1 https://github.com/techki545/MPP_Project.git ..\MPP_Project
python deploy\patch_frontend_graph.py --repo ..\MPP_Project

.\demo\start_demo.ps1 -UIrepo ..\MPP_Project -Port 8793
```

打开 http://127.0.0.1:8793/。

页面顶部会显示：

> **测试版 · 合成演示数据 · 不可用于临床**

## 预期

- 回答不应出现拒绝（除非问无关问题）
- 证据状态显示版本、缺口
- 治疗要素/缺口补检索可看到合成数据
- 页面显示红色“测试版”横幅

## 清理

```powershell
Remove-Item demo\data -Recurse -Force
Remove-Item demo\work -Recurse -Force
```

`demo/data` 和 `demo/work` 已被 `.gitignore` 忽略。
