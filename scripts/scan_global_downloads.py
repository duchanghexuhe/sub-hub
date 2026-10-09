"""扫描 Global 代理集中含 download/update 的域，供人工评估 NVIDIA 同款问题。"""
import re

doms = set()
with open("app/baseline_rules/Global.yaml", encoding="utf-8", errors="ignore") as f:
    for line in f:
        m = re.match(r"^\s*-\s+'?(\+\.?[^']+)'?\s*$", line)
        if m:
            doms.add(m.group(1).lstrip("+").lstrip(".").lower())

hits = sorted(d for d in doms if re.search(r"download|update|driver", d))
print(f"Global 含 download/update/driver 的域: {len(hits)}")
for d in hits:
    print(" ", d)
