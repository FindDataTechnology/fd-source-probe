#!/usr/bin/env bash
# 回滚 testaction003/build-pool 到覆盖前的占位（由 rehearse-slot.sh 于 2026-11-10T12:28:39Z 生成）
# 锚点：abea607 = fd-wanxing.git @ 53eeb8957 — snapshot
set -euo pipefail
cd /tmp && rm -rf slot-restore
git clone git@github.com:testaction003/build-pool.git slot-restore
cd slot-restore
git reset --hard abea607c8a2ff58806f0977403ad6e736db48334
git push --force origin main
echo "已回滚 testaction003/build-pool → abea607"
echo "注意：该占位本身处于失败态（见出片池演练报告），回滚等于回到同样的状态。"
