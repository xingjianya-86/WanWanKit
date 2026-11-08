#!/bin/sh
# WanWanKit 提交前密钥扫描门禁
# 用法：sh scripts/check_secrets.sh          # 扫描暂存区（git add 之后、commit 之前）
#       sh scripts/check_secrets.sh --all    # 扫描全工作区（除 tests/ 与本脚本）
# 命中即 exit 1 阻断提交。tests/ 下的 fixture 假值（如 "abc"）不扫描。
#
# 手动 git commit 时请先跑本脚本；pre-commit 钩子可直接引用本文件。

set -e

MODE="staged"
if [ "$1" = "--all" ]; then
  MODE="all"
fi

# 真实密钥形态（测试假值 plaintext-ark-123 / SESSDATA=x 不命中）
PAT='SESSDATA=[A-Za-z0-9%!\-]{20,}'
PAT="$PAT|bili_jct=[0-9a-f]{32}"
PAT="$PAT|DedeUserID=[0-9]{4,}"
PAT="$PAT|sk-[A-Za-z0-9]{20,}"
PAT="$PAT|-----BEGIN [A-Z ]*PRIVATE KEY"
PAT="$PAT|ark-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
PAT="$PAT|tp-[A-Za-z0-9]{24,}"
PAT="$PAT|Bearer [A-Za-z0-9_\-\.]{30,}"

if [ "$MODE" = "staged" ]; then
  # 只扫暂存的**新增/修改行**；排除 tests/（fixture 假值合法存在）
  HITS=$(git diff --cached | grep -E "$PAT" || true)
else
  HITS=$(git ls-files | grep -v -E '^(tests/|scripts/check_secrets\.sh)' | xargs grep -E -n "$PAT" 2>/dev/null || true)
fi

if [ -n "$HITS" ]; then
  echo "🔴 密钥扫描命中——疑似真实凭据，禁止提交："
  echo "$HITS" | head -15
  echo ""
  echo "如果这是误报（如测试 fixture），请调整 scripts/check_secrets.sh 的 PAT 或移出扫描范围。"
  exit 1
fi

echo "✅ 密钥扫描通过（模式：$MODE）"
