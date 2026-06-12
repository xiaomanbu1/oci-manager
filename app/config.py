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
    # 登录用户名 + 访问口令，password 留空则不鉴权（不建议公网裸跑）
    username: str = "admin"
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
    import logging
    clog = logging.getLogger("oci_manager.config")
    # 用 isfile：即使 Docker 把不存在的挂载点建成了目录，也当没配置处理，不崩
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    else:
        if os.path.isdir(path):
            clog.warning("%s 是目录（Docker 挂载坑），忽略，使用默认配置", path)
        else:
            clog.warning("未找到 %s，使用默认配置（账号请在网页「配置」页上传）", path)
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
        clog.error("加载存储账号失败: %s", e)

    tg_raw = raw.get("telegram", {}) or {}
    admin_ids = tg_raw.get("admin_ids", [])
    if isinstance(admin_ids, str):
        admin_ids = [x.strip() for x in admin_ids.split(",") if x.strip()]
    admin_ids = [str(x) for x in admin_ids]
    # 环境变量覆盖（可不依赖 config.yaml）
    env = os.environ.get
    if env("OCI_MANAGER_TG_ADMINS"):
        admin_ids = [x.strip() for x in env("OCI_MANAGER_TG_ADMINS").split(",") if x.strip()]
    telegram = TelegramConfig(
        enabled=_envbool("OCI_MANAGER_TG_ENABLED", tg_raw.get("enabled", False)),
        token=env("OCI_MANAGER_TG_TOKEN", tg_raw.get("token", "")),
        admin_ids=admin_ids,
    )

    web_raw = raw.get("web", {}) or {}
    web = WebConfig(
        enabled=_envbool("OCI_MANAGER_WEB_ENABLED", web_raw.get("enabled", True)),
        host=env("OCI_MANAGER_WEB_HOST", web_raw.get("host", "0.0.0.0")),
        port=int(env("OCI_MANAGER_WEB_PORT", web_raw.get("port", 9527))),
        username=env("OCI_MANAGER_WEB_USERNAME", web_raw.get("username", "admin")),
        password=env("OCI_MANAGER_WEB_PASSWORD", web_raw.get("password", "")),
    )

    # 网页设置（data/settings.json）覆盖环境变量/yaml，使网页改的设置重启后仍生效
    try:
        from . import store
        st = store.load_settings()
    except Exception:
        st = {}
    if st:
        if st.get("web_username"):
            web.username = st["web_username"]
        if "web_password" in st:
            web.password = st["web_password"]
        if "tg_enabled" in st:
            telegram.enabled = bool(st["tg_enabled"])
        if "tg_token" in st:
            telegram.token = st["tg_token"]
        if "tg_admin_ids" in st:
            ids = st["tg_admin_ids"]
            if isinstance(ids, str):
                ids = [x.strip() for x in ids.split(",") if x.strip()]
            telegram.admin_ids = [str(x) for x in ids]

    return AppConfig(accounts=accounts, telegram=telegram, web=web)


def _envbool(key: str, default: bool) -> bool:
    v = os.environ.get(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")
