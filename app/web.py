"""
Web 面板后端 (FastAPI)。所有云操作开成 JSON API，前端单页调用。
简单口令鉴权：登录拿一个内存 token，写 cookie。
"""
import os
import secrets
import logging
from functools import wraps

from fastapi import FastAPI, Request, HTTPException, Depends, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from .service import Service

log = logging.getLogger("oci_manager.web")
INDEX_HTML = os.path.join(os.path.dirname(__file__), "templates", "index.html")


class ActionReq(BaseModel):
    account: str
    instance_id: str
    action: str = ""


class GrabReq(BaseModel):
    account: str
    display_name: str
    subnet_id: str
    image_id: str
    shape: str
    ocpus: float | None = None
    memory_gb: float | None = None
    ssh_key: str | None = None
    boot_volume_gb: int | None = None
    retries: int = 100
    interval: int = 60


class LoginReq(BaseModel):
    username: str = ""
    password: str = ""


class TgVerifyReq(BaseModel):
    code: str


class AccountSettingReq(BaseModel):
    current_password: str = ""
    new_username: str | None = None
    new_password: str | None = None


class TgSettingReq(BaseModel):
    enabled: bool = False
    token: str = ""
    admin_ids: str = ""


class UserReq(BaseModel):
    account: str
    user_id: str


class UserCreateReq(BaseModel):
    account: str
    name: str
    email: str | None = None
    description: str | None = None
    group: str = "Administrators"


class ConfigDelReq(BaseModel):
    name: str


class AccountRenameReq(BaseModel):
    old: str
    new: str


class AccountDeleteBatchReq(BaseModel):
    names: list[str]


class RenameReq(BaseModel):
    account: str
    instance_id: str
    name: str


class ResizeReq(BaseModel):
    account: str
    instance_id: str
    ocpus: float
    memory_gb: float


class BootResizeReq(BaseModel):
    account: str
    instance_id: str
    size_gb: int | None = None
    vpu: int | None = None


async def _tg_broadcast(token: str, admins: list, text: str) -> bool:
    """通过 Telegram Bot API 给所有管理员发消息，任一成功即返回 True。"""
    import httpx
    ok = False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    async with httpx.AsyncClient(timeout=10) as c:
        for chat in admins:
            try:
                r = await c.post(url, json={"chat_id": chat, "text": text})
                if r.status_code == 200:
                    ok = True
                else:
                    log.warning("TG 发送到 %s 失败: %s %s", chat, r.status_code, r.text[:120])
            except Exception as e:
                log.warning("TG 发送到 %s 异常: %s", chat, e)
    return ok


def create_app(service: Service, config=None, password: str = "") -> FastAPI:
    app = FastAPI(title="OCI Manager")
    valid_tokens: set = set()

    # 可变鉴权状态：网页改设置时直接改这里，无需重启
    if config is not None:
        AUTH = {
            "username": config.web.username,
            "password": config.web.password,
            "tg_token": config.telegram.token if config.telegram.enabled else "",
            "tg_admins": [str(a) for a in (config.telegram.admin_ids or [])],
            "tg_enabled": bool(config.telegram.enabled),
        }
    else:
        AUTH = {"username": "admin", "password": password, "tg_token": "",
                "tg_admins": [], "tg_enabled": False}

    def tg_ok():
        return bool(AUTH["password"] and AUTH["tg_enabled"] and AUTH["tg_token"] and AUTH["tg_admins"])

    import time
    tg_codes: dict = {}
    last_send = {"t": 0.0}

    with open(INDEX_HTML, "r", encoding="utf-8") as f:
        _index_template = f.read()

    @app.exception_handler(Exception)
    async def biz_error(request: Request, exc: Exception):
        log.warning("API 异常 %s: %s", request.url.path, exc)
        return JSONResponse(status_code=400, content={"ok": False, "detail": str(exc)})

    def check_auth(request: Request):
        if not AUTH["password"]:
            return True
        token = request.cookies.get("oci_token", "")
        if token not in valid_tokens:
            raise HTTPException(status_code=401, detail="未登录")
        return True

    def _issue_token() -> JSONResponse:
        token = secrets.token_urlsafe(24)
        valid_tokens.add(token)
        resp = JSONResponse({"ok": True})
        resp.set_cookie("oci_token", token, httponly=True, samesite="lax")
        return resp

    @app.get("/", response_class=HTMLResponse)
    async def index():
        # 每次按当前鉴权状态渲染，设置改了立即生效
        page = (_index_template
                .replace("__AUTH_REQUIRED__", "true" if AUTH["password"] else "false")
                .replace("__TG_LOGIN__", "true" if tg_ok() else "false"))
        return HTMLResponse(page)

    @app.post("/api/login")
    async def login(req: LoginReq):
        if not AUTH["password"]:
            return _issue_token()
        user_ok = secrets.compare_digest(req.username or "", AUTH["username"] or "")
        pass_ok = secrets.compare_digest(req.password or "", AUTH["password"])
        if user_ok and pass_ok:
            return _issue_token()
        raise HTTPException(status_code=401, detail="用户名或口令错误")

    @app.post("/api/login/tg/send")
    async def tg_send():
        if not tg_ok():
            raise HTTPException(status_code=400, detail="未配置 Telegram 登录")
        now = time.time()
        if now - last_send["t"] < 55:
            raise HTTPException(status_code=429, detail="发送太频繁，请稍后再试")
        code = f"{secrets.randbelow(10**8):08d}"
        tg_codes.clear()
        tg_codes[code] = now + 300
        last_send["t"] = now
        text = (f"🔐 OCI Manager 登录验证码：{code}\n"
                f"5 分钟内有效。若非本人操作请忽略本条消息。")
        sent = await _tg_broadcast(AUTH["tg_token"], AUTH["tg_admins"], text)
        if not sent:
            raise HTTPException(status_code=500, detail="发送失败，请检查 bot token / admin_ids")
        return {"ok": True}

    @app.post("/api/login/tg/verify")
    async def tg_verify(req: TgVerifyReq):
        code = (req.code or "").strip()
        exp = tg_codes.get(code)
        if not exp or exp < time.time():
            raise HTTPException(status_code=401, detail="验证码错误或已过期")
        tg_codes.pop(code, None)
        return _issue_token()

    @app.post("/api/logout")
    async def logout(request: Request):
        token = request.cookies.get("oci_token", "")
        valid_tokens.discard(token)
        resp = JSONResponse({"ok": True})
        resp.delete_cookie("oci_token")
        return resp

    # ---------- 设置 ----------
    @app.get("/api/settings")
    async def get_settings(_=Depends(check_auth)):
        return {
            "username": AUTH["username"],
            "has_password": bool(AUTH["password"]),
            "tg_enabled": AUTH["tg_enabled"],
            "tg_token": AUTH["tg_token"],
            "tg_admin_ids": ",".join(AUTH["tg_admins"]),
            "tg_login_ready": tg_ok(),
        }

    def _persist():
        from . import store
        store.save_settings({
            "web_username": AUTH["username"],
            "web_password": AUTH["password"],
            "tg_enabled": AUTH["tg_enabled"],
            "tg_token": AUTH["tg_token"],
            "tg_admin_ids": AUTH["tg_admins"],
        })

    @app.post("/api/settings/account")
    async def set_account(req: AccountSettingReq, _=Depends(check_auth)):
        # 已设密码时，改密码需校验当前密码
        if AUTH["password"] and not secrets.compare_digest(req.current_password or "", AUTH["password"]):
            raise HTTPException(status_code=401, detail="当前密码不正确")
        if req.new_username is not None and req.new_username.strip():
            AUTH["username"] = req.new_username.strip()
        if req.new_password is not None:
            AUTH["password"] = req.new_password    # 允许设空=关闭鉴权
        _persist()
        valid_tokens.clear()                       # 改完所有人重新登录
        return {"ok": True, "msg": "已保存，请用新凭据重新登录"}

    @app.post("/api/settings/telegram")
    async def set_telegram(req: TgSettingReq, _=Depends(check_auth)):
        AUTH["tg_enabled"] = bool(req.enabled)
        AUTH["tg_token"] = (req.token or "").strip()
        ids = req.admin_ids or ""
        AUTH["tg_admins"] = [x.strip() for x in ids.replace("，", ",").split(",") if x.strip()]
        _persist()
        return {"ok": True, "msg": "Telegram 设置已保存", "tg_login_ready": tg_ok()}

    @app.post("/api/settings/telegram/test")
    async def test_telegram(_=Depends(check_auth)):
        if not (AUTH["tg_token"] and AUTH["tg_admins"]):
            raise HTTPException(status_code=400, detail="请先填写 token 和管理员 ID")
        ok = await _tg_broadcast(AUTH["tg_token"], AUTH["tg_admins"],
                                 "✅ OCI Manager 测试消息：你的 Telegram 配置正常！")
        if not ok:
            raise HTTPException(status_code=500, detail="发送失败，检查 token/ID，且你需先对 bot 发过 /start")
        return {"ok": True, "msg": "测试消息已发送，去 Telegram 查收"}

    @app.get("/api/accounts")
    async def accounts(_=Depends(check_auth)):
        return {"accounts": service.account_names()}

    @app.get("/api/overview")
    async def overview(_=Depends(check_auth)):
        return {"overview": service.overview_all()}

    @app.get("/api/profiles")
    async def profiles(_=Depends(check_auth)):
        return {"groups": service.profiles_grouped()}

    # ---------- 配置管理：网页上传 OCI 账号 ----------
    @app.get("/api/config/accounts")
    async def config_accounts(_=Depends(check_auth)):
        return {"accounts": service.config_accounts_summary()}

    @app.post("/api/config/oci")
    async def config_oci(config_text: str = Form(...),
                         files: list[UploadFile] = File(default=[]),
                         _=Depends(check_auth)):
        import asyncio
        pem_map = {}
        for f in files:
            pem_map[f.filename] = await f.read()
        res = await asyncio.to_thread(service.add_oci_config, config_text, pem_map)
        return {"ok": True, **res}

    @app.post("/api/config/delete")
    async def config_delete(req: ConfigDelReq, _=Depends(check_auth)):
        import asyncio
        await asyncio.to_thread(service.delete_account, req.name)
        return {"ok": True}

    @app.post("/api/account/rename")
    async def account_rename(req: AccountRenameReq, _=Depends(check_auth)):
        import asyncio
        await asyncio.to_thread(service.rename_account, req.old, req.new)
        return {"ok": True, "msg": f"已重命名为 {req.new}"}

    @app.post("/api/account/delete_batch")
    async def account_delete_batch(req: AccountDeleteBatchReq, _=Depends(check_auth)):
        import asyncio
        n = await asyncio.to_thread(service.delete_accounts, req.names)
        return {"ok": True, "msg": f"已删除 {n} 个账号"}

    @app.get("/api/account_overview")
    async def account_overview(account: str, months: int = 3, _=Depends(check_auth)):
        import asyncio
        data = await asyncio.to_thread(service.account_overview, account, months)
        return data

    @app.get("/api/instances")
    async def instances(account: str, _=Depends(check_auth)):
        import asyncio
        data = await asyncio.to_thread(service.list_instances, account)
        return {"instances": data}

    @app.post("/api/action")
    async def action(req: ActionReq, _=Depends(check_auth)):
        import asyncio
        msg = await asyncio.to_thread(
            service.power_action, req.account, req.instance_id, req.action)
        return {"ok": True, "msg": msg}

    @app.post("/api/chip")
    async def chip(req: ActionReq, _=Depends(check_auth)):
        import asyncio
        msg = await asyncio.to_thread(service.change_ip, req.account, req.instance_id)
        return {"ok": True, "msg": msg}

    @app.post("/api/terminate")
    async def terminate(req: ActionReq, _=Depends(check_auth)):
        import asyncio
        preserve = req.action == "preserve"   # action=preserve 表示保留启动盘
        msg = await asyncio.to_thread(service.terminate, req.account, req.instance_id, preserve)
        return {"ok": True, "msg": msg}

    @app.post("/api/instance/rename")
    async def inst_rename(req: RenameReq, _=Depends(check_auth)):
        import asyncio
        msg = await asyncio.to_thread(service.rename_instance, req.account, req.instance_id, req.name)
        return {"ok": True, "msg": msg}

    @app.post("/api/instance/resize")
    async def inst_resize(req: ResizeReq, _=Depends(check_auth)):
        import asyncio
        msg = await asyncio.to_thread(service.resize_instance, req.account, req.instance_id, req.ocpus, req.memory_gb)
        return {"ok": True, "msg": msg}

    @app.get("/api/instance/boot_volume")
    async def inst_bootvol(account: str, instance_id: str, _=Depends(check_auth)):
        import asyncio
        data = await asyncio.to_thread(service.get_boot_volume, account, instance_id)
        return data

    @app.post("/api/instance/resize_boot")
    async def inst_resize_boot(req: BootResizeReq, _=Depends(check_auth)):
        import asyncio
        msg = await asyncio.to_thread(service.resize_boot_volume, req.account, req.instance_id, req.size_gb, req.vpu)
        return {"ok": True, "msg": msg}

    @app.get("/api/instance/ssh")
    async def inst_ssh(account: str, instance_id: str, _=Depends(check_auth)):
        import asyncio
        data = await asyncio.to_thread(service.ssh_info, account, instance_id)
        return data

    # ---------- 用户管理 ----------
    @app.get("/api/users")
    async def users(account: str, _=Depends(check_auth)):
        import asyncio
        data = await asyncio.to_thread(service.list_users, account)
        return {"users": data}

    @app.post("/api/user/create")
    async def user_create(req: UserCreateReq, _=Depends(check_auth)):
        import asyncio
        res = await asyncio.to_thread(
            service.create_user, req.account, req.name, req.email,
            req.description, req.group)
        return {"ok": True, **res}

    @app.post("/api/user/delete")
    async def user_delete(req: UserReq, _=Depends(check_auth)):
        import asyncio
        msg = await asyncio.to_thread(service.delete_user, req.account, req.user_id)
        return {"ok": True, "msg": msg}

    @app.post("/api/user/reset_password")
    async def user_reset(req: UserReq, _=Depends(check_auth)):
        import asyncio
        pw = await asyncio.to_thread(service.reset_password, req.account, req.user_id)
        return {"ok": True, "password": pw}

    @app.post("/api/user/clear_2fa")
    async def user_clear2fa(req: UserReq, _=Depends(check_auth)):
        import asyncio
        n = await asyncio.to_thread(service.clear_user_2fa, req.account, req.user_id)
        return {"ok": True, "cleared": n}

    @app.post("/api/users/clear_all_2fa")
    async def clear_all_2fa(req: ActionReq, _=Depends(check_auth)):
        import asyncio
        res = await asyncio.to_thread(service.clear_all_2fa, req.account)
        return {"ok": True, **res}

    @app.get("/api/ads")
    async def ads(account: str, _=Depends(check_auth)):
        import asyncio
        data = await asyncio.to_thread(lambda: service.manager(account).list_availability_domains())
        return {"ads": data}

    @app.post("/api/grab")
    async def grab(req: GrabReq, _=Depends(check_auth)):
        params = {
            "display_name": req.display_name, "subnet_id": req.subnet_id,
            "image_id": req.image_id, "shape": req.shape,
            "ocpus": req.ocpus, "memory_gb": req.memory_gb,
            "ssh_key": req.ssh_key, "boot_volume_gb": req.boot_volume_gb,
            "retries": req.retries, "interval": req.interval,
        }
        job_id = service.start_grab(req.account, params)
        return {"ok": True, "job_id": job_id}

    @app.get("/api/jobs")
    async def jobs(_=Depends(check_auth)):
        return {"jobs": service.list_jobs()}

    @app.get("/api/job/{job_id}")
    async def job(job_id: str, _=Depends(check_auth)):
        j = service.get_job(job_id)
        if not j:
            raise HTTPException(status_code=404, detail="任务不存在")
        return j

    @app.post("/api/job/{job_id}/stop")
    async def stop_job(job_id: str, _=Depends(check_auth)):
        return {"ok": service.stop_grab(job_id)}

    return app
