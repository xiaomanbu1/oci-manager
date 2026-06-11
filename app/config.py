"""
配置加载：从 config.yaml 读取多个甲骨文账号、Telegram、Web 设置。
私钥既可以写文件路径，也可以直接把 PEM 内容塞进 key_content 字段。
"""
import os
import yaml
from dataclasses import dataclass, field
from typing import Optional

CONFIG_PATH = os.environ.get("OCI_MANAGER_CONFIG", "config.yaml")


@dataclass
class OciAccount:
    name: str                       # 账号别名，Bot/Web 里用它来区分
    user: str                       # user OCID
    tenancy: str                    # tenancy OCID
    fingerprint: str                # API key 指纹
    region: str                     # 默认区域，如 ap-tokyo-1
    key_file: Optional[str] = None  # 私钥文件路径
    key_content: Optional[str] = None  # 或直接内联 PEM 内容
    pass_phrase: Optional[str] = None
    compartment: Optional[str] = None  # 不填默认用 tenancy 根 compartment

    def to_oci_config(self) -> dict:
        """转成 oci SDK 认证用的 dict。"""
        cfg = {
            "user": self.user,
            "tenancy": self.tenancy,
            "fingerprint": self.fingerprint,
            "region": self.region,
        }
        if self.pass_phrase:
            cfg["pass_phrase"] = self.pass_phrase
        if self.key_content:
            cfg["key_content"] = self.key_content
        elif self.key_file:
            cfg["key_file"] = os.path.expanduser(self.key_file)
        else:
            raise ValueError(f"账号 {self.name} 缺少 key_file 或 key_content")
        return cfg

    @property
    def compartment_id(self) -> str:
        return self.compartment or self.tenancy


@dataclass
class TelegramConfig:
    enabled: bool = False
    token: str = ""
    # 只有白名单里的 chat_id 能操作，逗号分隔字符串或列表都可以
    admin_ids: list = field(default_factory=list)


@dataclass
class WebConfig:
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 9527
    # 简单的访问口令，留空则不鉴权（不建议公网裸跑）
    password: str = ""


@dataclass
class AppConfig:
    accounts: list = field(default_factory=list)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    web: WebConfig = field(default_factory=WebConfig)

    def get_account(self, name: str) -> Optional[OciAccount]:
        for a in self.accounts:
            if a.name == name:
                return a
        return None


def load_config(path: str = None) -> AppConfig:
    path = path or CONFIG_PATH
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    else:
        # 没有 config.yaml 也能起：Web 默认开，账号靠网页上传
        import logging
        logging.getLogger("oci_manager.config").warning(
            "未找到 %s，使用默认配置（Web 开启，账号请在网页「配置」页上传）", path)
        raw = {}

    accounts = []
    for item in raw.get("accounts", []):
        accounts.append(OciAccount(**item))

    # 合并网页上传、持久化在 data/ 的账号
    try:
        from . import store
        for item in store.load_accounts():
            accounts = [a for a in accounts if a.name != item.get("name")]
            accounts.append(OciAccount(
                name=item["name"], user=item["user"], tenancy=item["tenancy"],
                fingerprint=item["fingerprint"], region=item["region"],
                key_content=item.get("key_content"),
                pass_phrase=item.get("pass_phrase"),
                compartment=item.get("compartment"),
            ))
    except Exception as e:
        import logging
        logging.getLogger("oci_manager.config").error("加载存储账号失败: %s", e)

    tg_raw = raw.get("telegram", {}) or {}
    admin_ids = tg_raw.get("admin_ids", [])
    if isinstance(admin_ids, str):
        admin_ids = [x.strip() for x in admin_ids.split(",") if x.strip()]
    admin_ids = [str(x) for x in admin_ids]
    telegram = TelegramConfig(
        enabled=tg_raw.get("enabled", False),
        token=tg_raw.get("token", ""),
        admin_ids=admin_ids,
    )

    web_raw = raw.get("web", {}) or {}
    web = WebConfig(
        enabled=web_raw.get("enabled", True),
        host=web_raw.get("host", "0.0.0.0"),
        port=int(web_raw.get("port", 9527)),
        password=web_raw.get("password", ""),
    )

    return AppConfig(accounts=accounts, telegram=telegram, web=web)
