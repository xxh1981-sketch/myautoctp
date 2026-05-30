# 三仓版本兼容说明

AutoCTP 与 **autotrade**、**autostraggle** 为独立 git 仓库。编排层若调用策略库新接口，须三仓配套发布。

## 推荐流程

1. **策略库先 push**（接口/签名有变时）
2. **再 push autoctp**
3. 本机按顺序 pull 后跑全量测试，再启 `merged_main.py`

```powershell
cd D:\autotrade;      git pull
cd D:\autostraggle;   git pull
cd D:\autoctp;        git pull

$env:AUTOTRADE_ROOT = 'D:\autotrade'
$env:AUTOSTRAGGLE_ROOT = 'D:\autostraggle'
pytest D:\autoctp\tests\ -q
```

## 已验证组合（本地维护）

| 日期 | autoctp | autotrade | autostraggle | 备注 |
|------|---------|-----------|--------------|------|
| （填写） | `git rev-parse --short HEAD` | … | … | 大版本上线前记录 |
| 2026-05-30 | `ae4fca7` | `45392a4` | `5b164c4` | 新增日志降噪过滤器（节流重复 VIX 日志 / 降级预期撤单回报），全量 unit 测试通过 |

## 兼容锁（启动自检）

仓库内提供 `docs/compat_lock.yaml`：

- 填写 `expected_commits.autoctp/autotrade/autostraggle`
- 启动时会打印三仓实际 commit 并比对
- `compat_lock_enforce=true` 时，不匹配将拒绝启动（exit code 5）

```powershell
# 在三仓根目录各执行，将输出填入上表
git rev-parse --short HEAD
git log -1 --oneline
```

## CI 说明

`pytest-full` 对策略库使用 **`git clone --depth 1` 默认分支 HEAD**，不会自动 pin 到上表 commit。若仅 push autoctp 而策略库未更新，full job 可能失败——属预期，按 §推荐流程 处理。

本地检查 secrets 与路径：

```powershell
python scripts/check_ci_readiness.py
python scripts/check_ci_readiness.py --strict-ci
```
