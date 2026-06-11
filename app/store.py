"""
运行时账号存储：网页上添加的 OCI 账号存到 data/accounts.json，重启后自动加载。
私钥以 key_content 内联保存（只需挂载一个 data 目录即可持久化）。
configparser 解析用户粘贴的 OCI 标准配置文本（可含多个 profile）。
"""
import os
import json
import logging
import configparser
from typing import List, Dict

log = logging.getLogger("oci_manager.store")

DATA_DIR = os.environ.get("OCI_MANAGER_DATA", "data")
ACCOUNTS_FILE = os.path.join(DATA_DIR, "accounts.json")


def _ensure_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def load_accounts() -> List[Dict]:
    if not os.path.exists(ACCOUNTS_FILE):
        return []
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.error("读取 accounts.json 失败: %s", e)
        return []


def save_accounts(accounts: List[Dict]):
    _ensure_dir()
    tmp = ACCOUNTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(accounts, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ACCOUNTS_FILE)  # 原子替换，避免写一半损坏


def remove_account(name: str) -> bool:
    accounts = load_accounts()
    new = [a for a in accounts if a.get("name") != name]
    if len(new) == len(accounts):
        return False
    save_accounts(new)
    return True


def parse_oci_config(text: str) -> List[Dict]:
    """
    解析 OCI 标准配置文本（~/.oci/config 格式，可含多个 [profile]）。
    [DEFAULT] 也当普通 section。返回带 key_file_ref（用于和上传 PEM 匹配）。
    """
    cp = configparser.ConfigParser(default_section="__no_default__",
                                   interpolation=None)
    try:
        cp.read_string(text)
    except configparser.Error as e:
        raise ValueError(f"配置文本解析失败: {e}")

    out = []
    for sec in cp.sections():
        s = cp[sec]
        kf = (s.get("key_file") or "").strip()
        out.append({
            "name": sec.strip(),
            "user": (s.get("user") or "").strip(),
            "tenancy": (s.get("tenancy") or "").strip(),
            "fingerprint": (s.get("fingerprint") or "").strip(),
            "region": (s.get("region") or "").strip(),
            "key_file_ref": os.path.basename(kf) if kf else "",
            "pass_phrase": (s.get("pass_phrase") or "").strip() or None,
            "compartment": (s.get("compartment") or "").strip() or None,
        })
    if not out:
        raise ValueError("没解析到任何 profile，至少要有一个 [section]")
    return out


def add_profiles(profiles: List[Dict], pem_map: Dict[str, bytes]) -> Dict:
    """
    保存 profile 并为每个匹配 PEM（key_content 内联）。
    匹配：① profile 的 key_file 文件名命中上传文件名；② 否则只传了一个 PEM 就全套用。
    返回 {added:[...], skipped:[{name,reason}]}
    """
    existing = {a["name"]: a for a in load_accounts()}
    added, skipped = [], []
    pem_items = list(pem_map.items())

    for p in profiles:
        if not (p["user"] and p["tenancy"] and p["fingerprint"] and p["region"]):
            skipped.append({"name": p["name"],
                            "reason": "user/tenancy/fingerprint/region 不全"})
            continue

        pem_bytes = None
        if p["key_file_ref"] and p["key_file_ref"] in pem_map:
            pem_bytes = pem_map[p["key_file_ref"]]
        elif len(pem_items) == 1:
            pem_bytes = pem_items[0][1]

        if not pem_bytes:
            skipped.append({"name": p["name"], "reason": "未匹配到 PEM 密钥"})
            continue

        try:
            key_content = pem_bytes.decode("utf-8")
        except UnicodeDecodeError:
            skipped.append({"name": p["name"], "reason": "PEM 不是有效文本"})
            continue

        existing[p["name"]] = {
            "name": p["name"], "user": p["user"], "tenancy": p["tenancy"],
            "fingerprint": p["fingerprint"], "region": p["region"],
            "key_content": key_content, "pass_phrase": p["pass_phrase"],
            "compartment": p["compartment"],
        }
        added.append(p["name"])

    save_accounts(list(existing.values()))
    return {"added": added, "skipped": skipped}
