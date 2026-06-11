"""
服务层：持有所有账号的 OciManager，管理后台抢机任务。
Bot 和 Web 共用同一个 Service 单例，状态一致。
"""
import uuid
import time
import threading
import logging
from typing import Dict, List, Optional

from .config import AppConfig
from .oci_client import OciManager

log = logging.getLogger("oci_manager.service")


class GrabJob:
    """一个后台抢机任务的状态。"""
    def __init__(self, job_id: str, account: str, params: dict):
        self.id = job_id
        self.account = account
        self.params = params
        self.status = "running"   # running / success / failed / stopped
        self.attempts = 0
        self.logs: List[str] = []
        self.result: Optional[dict] = None
        self.created = time.time()
        self._stop = threading.Event()

    def log_line(self, attempt: int, ad: str, msg: str):
        self.attempts = attempt
        line = f"[{time.strftime('%H:%M:%S')}] #{attempt} {ad.split(':')[-1]} {msg}"
        self.logs.append(line)
        self.logs = self.logs[-200:]  # 只留最近 200 行
        log.info("grab %s %s", self.id[:8], line)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "account": self.account, "status": self.status,
            "attempts": self.attempts, "result": self.result,
            "logs": self.logs[-50:], "params": self.params,
        }


class Service:
    def __init__(self, config: AppConfig):
        self.config = config
        self._managers: Dict[str, OciManager] = {}
        self._errors: Dict[str, str] = {}
        self.jobs: Dict[str, GrabJob] = {}
        self._init_managers()

    def _init_managers(self):
        for acc in self.config.accounts:
            try:
                self._managers[acc.name] = OciManager(acc)
                log.info("账号 %s 初始化成功 (%s)", acc.name, acc.region)
            except Exception as e:
                self._errors[acc.name] = str(e)
                log.error("账号 %s 初始化失败: %s", acc.name, e)

    def account_names(self) -> List[str]:
        return list(self._managers.keys())

    def manager(self, account: str) -> OciManager:
        if account not in self._managers:
            raise KeyError(f"账号 {account} 不存在或初始化失败: "
                           f"{self._errors.get(account, '未知')}")
        return self._managers[account]

    # ---------- 直接转发的同步操作 ----------
    def list_instances(self, account: str):
        return self.manager(account).list_instances()

    def power_action(self, account: str, instance_id: str, action: str):
        return self.manager(account).power_action(instance_id, action)

    def terminate(self, account: str, instance_id: str, preserve_boot: bool = False):
        return self.manager(account).terminate(instance_id, preserve_boot)

    def change_ip(self, account: str, instance_id: str):
        return self.manager(account).change_public_ip(instance_id)

    # ---------- 用户管理 ----------
    def list_users(self, account: str):
        return self.manager(account).list_users()

    def create_user(self, account: str, name: str, email: str = None,
                    description: str = None, group: str = "Administrators"):
        return self.manager(account).create_user(name, email, description, group)

    def delete_user(self, account: str, user_id: str):
        return self.manager(account).delete_user(user_id)

    def reset_password(self, account: str, user_id: str):
        return self.manager(account).reset_password(user_id)

    def clear_user_2fa(self, account: str, user_id: str):
        return self.manager(account).clear_user_2fa(user_id)

    def clear_all_2fa(self, account: str):
        return self.manager(account).clear_all_2fa()

    def overview_all(self) -> List[dict]:
        out = []
        for name in self.account_names():
            try:
                out.append(self.manager(name).overview())
            except Exception as e:
                out.append({"account": name, "error": str(e)})
        return out

    def account_overview(self, account: str, months: int = 3) -> dict:
        return self.manager(account).account_overview(months)

    def profiles_grouped(self) -> List[dict]:
        """Profile 列表按区域分组：[{region, accounts:[{name, tenancy, state}]}]"""
        groups: Dict[str, list] = {}
        for acc in self.config.accounts:
            entry = {"name": acc.name, "user": acc.user,
                     "region": acc.region, "ok": acc.name in self._managers}
            if entry["ok"]:
                try:
                    entry["tenancy_name"] = self._managers[acc.name].tenancy_name()
                except Exception:
                    entry["tenancy_name"] = ""
            else:
                entry["error"] = self._errors.get(acc.name, "初始化失败")
            groups.setdefault(acc.region, []).append(entry)
        return [{"region": r, "accounts": groups[r]} for r in sorted(groups)]

    # ---------- 抢机任务 ----------
    def start_grab(self, account: str, params: dict) -> str:
        mgr = self.manager(account)
        job_id = uuid.uuid4().hex
        job = GrabJob(job_id, account, params)
        self.jobs[job_id] = job

        def worker():
            def on_attempt(i, ad, msg):
                if job._stop.is_set():
                    raise _StopGrab()
                job.log_line(i, ad, msg)
            try:
                res = mgr.grab(on_attempt=on_attempt, **params)
                job.result = res
                job.status = "success" if res.get("ok") else "failed"
            except _StopGrab:
                job.status = "stopped"
                job.logs.append("已手动停止")
            except Exception as e:
                job.status = "failed"
                job.result = {"ok": False, "error": str(e)}
                job.logs.append(f"任务异常: {e}")

        threading.Thread(target=worker, daemon=True).start()
        return job_id

    def stop_grab(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job and job.status == "running":
            job._stop.set()
            return True
        return False

    def list_jobs(self) -> List[dict]:
        return [j.to_dict() for j in sorted(
            self.jobs.values(), key=lambda x: x.created, reverse=True)]

    def get_job(self, job_id: str) -> Optional[dict]:
        j = self.jobs.get(job_id)
        return j.to_dict() if j else None


class _StopGrab(Exception):
    pass
