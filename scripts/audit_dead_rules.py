"""排查 ChinaMax 直连条目被前位代理集抢先的死条目。

模拟 Clash 规则链首中语义：按 manifest 顺序对每个域名条目找第一条命中的规则，
统计"ChinaMax 本意直连、实际首次命中却是代理组"的条目。
"""
import re
from collections import Counter

BASE = "app/baseline_rules"

# manifest 实际顺序（域名集部分），policy 取"默认走向"判断是否代理侧
CHAIN = [
    ("steam-download", "steam-download.yaml", "DIRECT"),
    ("steam-extra", "steam-extra.yaml", "PROXY"),
    ("Epic", "Epic.yaml", "GAME"), ("Riot", "Riot.yaml", "GAME"),
    ("Blizzard", "Blizzard.yaml", "GAME"), ("EA", "EA.yaml", "GAME"),
    ("Origin", "Origin.yaml", "GAME"), ("Ubisoft", "Ubisoft.yaml", "GAME"),
    ("PlayStation", "PlayStation.yaml", "GAME"), ("Xbox", "Xbox.yaml", "GAME"),
    ("Nintendo", "Nintendo.yaml", "GAME"),
    ("GitHub", "GitHub.yaml", "PROXY"), ("Google", "Google.yaml", "PROXY"),
    ("Microsoft", "Microsoft.yaml", "MS"), ("Apple", "Apple.yaml", "APPLE"),
    ("Global", "Global.yaml", "PROXY"), ("ProxyGFWlist", "ProxyGFWlist.yaml", "PROXY"),
    ("ChinaMax", "ChinaMax.yaml", "DIRECT"),
]


def load_yaml_domains(path):
    """blackmatrix7 classical/domain yaml payload：'+.dom' 后缀 或 'dom' 裸域。
    classical 里还有 DOMAIN-SUFFIX,dom / DOMAIN,dom 行式。"""
    out = set()
    pat_kv = re.compile(r"^\s*-\s+(?:'|\")?(?:\+\.)?([^'\"\s]+)(?:'|\")?\s*$")
    pat_ds = re.compile(r"^\s*-\s+(?:DOMAIN(?:-SUFFIX)?),\s*([^,\s]+)")
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.rstrip("\r\n")
            if not line or line.lstrip().startswith("#"):
                continue
            m = pat_kv.match(line)
            if m and not line.lstrip().startswith("- DOMAIN"):
                out.add(m.group(1).lower().lstrip("."))
                continue
            m = pat_ds.match(line)
            if m:
                out.add(m.group(1).lower().lstrip("."))
    return out


def load_list_domains(path):
    out = set()
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            d = line.strip().lstrip(".").lower()
            if d and not d.startswith("#") and not d.startswith("["):
                out.add(d)
    return out


def suffix_match(domain, suffix_set):
    """domain==suf 或 domain 以 '.suf' 结尾。"""
    parts = domain.split(".")
    for i in range(len(parts)):
        suf = ".".join(parts[i:])
        if suf in suffix_set:
            return True
    return False


sets = {}
for name, fname, pol in CHAIN:
    if fname.endswith(".yaml"):
        sets[name] = (load_yaml_domains(f"{BASE}/{fname}"), pol)
    else:
        sets[name] = (load_list_domains(f"{BASE}/{fname}"), pol)

print("各集条目数:", {k: len(v[0]) for k, v in sets.items()})

china = load_list_domains(f"{BASE}/ChinaMax.list")
print("ChinaMax.list 条目数:", len(china))

dead = []
for dom in china:
    for name, _fname, pol in CHAIN:
        if suffix_match(dom, sets[name][0]):
            if name == "ChinaMax":
                break  # 正常：本意直连生效
            dead.append((dom, name, pol))
            break

print("\n死条目数（本意直连、实际首中代理侧）:", len(dead))
cnt = Counter((n, p) for _, n, p in dead)
print("\n按首中规则统计:")
for (n, p), c in cnt.most_common():
    print(f"  {c:4d}  {n} -> {p}")

# 只关心真实代理侧（GAME/MS/APPLE 默认 DIRECT 不算）
real_dead = [d for d in dead if d[2] == "PROXY"]
print("\n真实死条目（首中 🚀 代理）:", len(real_dead))
for dom, n, p in sorted(real_dead):
    print(f"  {dom}   (首中 {n})")
