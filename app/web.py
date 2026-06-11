"""
Web 面板后端 (FastAPI)。所有云操作开成 JSON API，前端单页调用。
简单口令鉴权：登录拿一个内存 token，写 cookie。
"""
import os
import secrets
import logging
from functools import wraps

from fastapi import FastAPI, Request, HTTPException, Depends
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
    password: str


class UserReq(BaseModel):
    account: str
    user_id: str


class UserCreateReq(BaseModel):
    account: str
    name: str
    email: str | None = None
    description: str | None = None
    group: str = "Administrators"


def create_app(service: Service, password: str = "") -> FastAPI:
    app = FastAPI(title="OCI Manager")
    valid_tokens: set = set()

    with open(INDEX_HTML, "r", encoding="utf-8") as f:
        _index_template = f.read()
    index_page = _index_template.replace(
        "__AUTH_REQUIRED__", "true" if password else "false")

    @app.exception_handler(Exception)
    async def biz_error(request: Request, exc: Exception):
        # 业务/SDK 异常统一回成 JSON，前端读 detail 字段提示
        log.warning("API 异常 %s: %s", request.url.path, exc)
        return JSONResponse(status_code=400, content={"ok": False, "detail": str(exc)})

    def check_auth(request: Request):
        if not password:
            return True
        token = request.cookies.get("oci_token", "")
        if token not in valid_tokens:
            raise HTTPException(status_code=401, detail="未登录")
        return True

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return HTMLResponse(index_page)

    @app.post("/api/login")
    async def login(req: LoginReq):
        if not password or secrets.compare_digest(req.password, password):
            token = secrets.token_urlsafe(24)
            valid_tokens.add(token)
            resp = JSONResponse({"ok": True})
            resp.set_cookie("oci_token", token, httponly=True, samesite="lax")
            return resp
        raise HTTPException(status_code=401, detail="口令错误")

    @app.get("/api/accounts")
    async def accounts(_=Depends(check_auth)):
        return {"accounts": service.account_names()}

    @app.get("/api/overview")
    async def overview(_=Depends(check_auth)):
        return {"overview": service.overview_all()}

    @app.get("/api/profiles")
    async def profiles(_=Depends(check_auth)):
        return {"groups": service.profiles_grouped()}

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
        msg = await asyncio.to_thread(service.terminate, req.account, req.instance_id)
        return {"ok": True, "msg": msg}

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
