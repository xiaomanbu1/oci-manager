"""
甲骨文云核心操作封装。
对外暴露干净的方法，Bot 和 Web 都调它。OCI SDK 是同步的，
上层用线程池跑，避免阻塞 asyncio。
"""
import time
import logging
import datetime
from typing import List, Dict, Optional

import oci

from .config import OciAccount

log = logging.getLogger("oci_manager.client")

# 抢机时这些错误码代表「容量不足/限流」，应该继续重试
RETRYABLE_CODES = {"InternalError", "Out of host capacity", "TooManyRequests",
                   "LimitExceeded", "QuotaExceeded"}


class OciManager:
    """单个甲骨文账号的操作句柄。"""

    def __init__(self, account: OciAccount):
        self.account = account
        cfg = account.to_oci_config()
        # 先校验配置合法性，错了早点报出来
        oci.config.validate_config(cfg)
        self.config = cfg
        self.compute = oci.core.ComputeClient(cfg)
        self.network = oci.core.VirtualNetworkClient(cfg)
        self.identity = oci.identity.IdentityClient(cfg)
        self.bs = oci.core.BlockstorageClient(cfg)
        self.limits = oci.limits.LimitsClient(cfg)
        self._usage = None          # 懒加载，指向 home region
        self._home_region = None
        self._tenancy_name = None

    @property
    def compartment_id(self) -> str:
        return self.account.compartment_id

    # ---------- 查询 ----------
    def list_availability_domains(self) -> List[str]:
        ads = self.identity.list_availability_domains(self.compartment_id).data
        return [ad.name for ad in ads]

    def list_instances(self) -> List[Dict]:
        """列出 compartment 下所有未终止的实例，附带公网 IP。"""
        instances = oci.pagination.list_call_get_all_results(
            self.compute.list_instances, self.compartment_id
        ).data
        result = []
        for inst in instances:
            if inst.lifecycle_state == "TERMINATED":
                continue
            pub_ip = self._get_instance_public_ip(inst.id)
            shape_cfg = inst.shape_config
            bv = self._get_boot_volume_brief(inst.id, inst.availability_domain)
            result.append({
                "id": inst.id,
                "name": inst.display_name,
                "state": inst.lifecycle_state,
                "shape": inst.shape,
                "ocpus": getattr(shape_cfg, "ocpus", None),
                "memory_gb": getattr(shape_cfg, "memory_in_gbs", None),
                "ad": inst.availability_domain,
                "region": inst.region,
                "public_ip": pub_ip,
                "boot_gb": bv.get("size_gb"),
                "boot_vpu": bv.get("vpu"),
                "time_created": str(inst.time_created),
            })
        return result

    def _get_boot_volume_brief(self, instance_id: str, ad: str) -> Dict:
        try:
            atts = self.compute.list_boot_volume_attachments(
                ad, self.compartment_id, instance_id=instance_id).data
            for a in atts:
                if a.lifecycle_state == "ATTACHED":
                    bv = self.bs.get_boot_volume(a.boot_volume_id).data
                    return {"id": bv.id, "size_gb": int(bv.size_in_gbs),
                            "vpu": int(bv.vpus_per_gb)}
        except Exception as e:
            log.debug("取启动盘失败 %s: %s", instance_id, e)
        return {}

    def get_instance(self, instance_id: str):
        return self.compute.get_instance(instance_id).data

    # ---------- 电源操作 ----------
    def power_action(self, instance_id: str, action: str) -> str:
        """action: START / STOP / SOFTRESET / RESET / SOFTSTOP"""
        action = action.upper()
        valid = {"START", "STOP", "SOFTRESET", "RESET", "SOFTSTOP"}
        if action not in valid:
            raise ValueError(f"不支持的操作 {action}")
        self.compute.instance_action(instance_id, action)
        return f"已下发 {action} 指令"

    def terminate(self, instance_id: str, preserve_boot_volume: bool = False) -> str:
        self.compute.terminate_instance(
            instance_id, preserve_boot_volume=preserve_boot_volume
        )
        return "已下发终止指令" + ("（保留启动盘）" if preserve_boot_volume else "")

    # ---------- 实例其它操作 ----------
    def rename_instance(self, instance_id: str, new_name: str) -> str:
        self.compute.update_instance(
            instance_id,
            oci.core.models.UpdateInstanceDetails(display_name=new_name))
        return f"已重命名为 {new_name}"

    def resize_instance(self, instance_id: str, ocpus: float, memory_gb: float) -> str:
        """改弹性规格的 OCPU/内存（A1.Flex / E*.Flex）。固定规格不支持。"""
        self.compute.update_instance(
            instance_id,
            oci.core.models.UpdateInstanceDetails(
                shape_config=oci.core.models.UpdateInstanceShapeConfigDetails(
                    ocpus=ocpus, memory_in_gbs=memory_gb)))
        return f"已下发升降级：{ocpus} OCPU / {memory_gb}GB（重启后生效）"

    def get_boot_volume(self, instance_id: str) -> Dict:
        inst = self.compute.get_instance(instance_id).data
        bv = self._get_boot_volume_brief(instance_id, inst.availability_domain)
        if not bv:
            raise RuntimeError("找不到启动盘")
        return bv

    def resize_boot_volume(self, instance_id: str, size_gb: int = None,
                           vpu: int = None) -> str:
        """扩容启动盘 / 调整性能 VPU。容量只能增不能减。"""
        bv = self.get_boot_volume(instance_id)
        details = {}
        if size_gb:
            details["size_in_gbs"] = int(size_gb)
        if vpu is not None:
            details["vpus_per_gb"] = int(vpu)
        self.bs.update_boot_volume(
            bv["id"], oci.core.models.UpdateBootVolumeDetails(**details))
        return "已下发启动盘调整（扩容后需在系统内扩展分区）"

    def ssh_info(self, instance_id: str) -> Dict:
        """给出 SSH 连接建议（用户名按镜像猜，OCI 常见 opc/ubuntu）。"""
        ip = self._get_instance_public_ip(instance_id)
        return {"ip": ip, "users": ["ubuntu", "opc", "root"]}

    # ---------- 换 IP ----------
    def _get_primary_vnic_and_private_ip(self, instance_id: str):
        """拿到实例主网卡 VNIC 和主私网 IP 对象。"""
        attachments = self.compute.list_vnic_attachments(
            self.compartment_id, instance_id=instance_id
        ).data
        for att in attachments:
            if att.lifecycle_state != "ATTACHED":
                continue
            vnic = self.network.get_vnic(att.vnic_id).data
            if not vnic.is_primary:
                continue
            priv_ips = self.network.list_private_ips(vnic_id=vnic.id).data
            for p in priv_ips:
                if p.is_primary:
                    return vnic, p
        return None, None

    def _get_instance_public_ip(self, instance_id: str) -> Optional[str]:
        try:
            vnic, _ = self._get_primary_vnic_and_private_ip(instance_id)
            return vnic.public_ip if vnic else None
        except Exception as e:  # 查 IP 失败不该让整个列表崩掉
            log.warning("取公网IP失败 %s: %s", instance_id, e)
            return None

    def change_public_ip(self, instance_id: str) -> str:
        """删掉当前临时公网 IP，重新分配一个新的。"""
        vnic, priv_ip = self._get_primary_vnic_and_private_ip(instance_id)
        if not vnic or not priv_ip:
            raise RuntimeError("找不到主网卡/私网IP")

        old_ip = vnic.public_ip
        # 找到挂在这个私网 IP 上的临时公网 IP
        try:
            cur = self.network.get_public_ip_by_private_ip_id(
                oci.core.models.GetPublicIpByPrivateIpIdDetails(private_ip_id=priv_ip.id)
            ).data
            if cur and cur.lifetime == "EPHEMERAL":
                self.network.delete_public_ip(cur.id)
                # 等旧 IP 真正释放
                self._wait_public_ip_gone(priv_ip.id)
        except oci.exceptions.ServiceError as e:
            if e.status != 404:  # 404 = 本来就没有公网IP，直接往下分配
                raise

        # 分配新的临时公网 IP
        new_pub = self.network.create_public_ip(
            oci.core.models.CreatePublicIpDetails(
                compartment_id=self.compartment_id,
                lifetime="EPHEMERAL",
                private_ip_id=priv_ip.id,
            )
        ).data
        # create 是异步的，轮询拿到真实 IP 地址
        new_ip = self._wait_public_ip_assigned(new_pub.id)
        return f"换IP成功：{old_ip or '无'} → {new_ip}"

    def _wait_public_ip_gone(self, private_ip_id: str, timeout: int = 30):
        for _ in range(timeout):
            try:
                self.network.get_public_ip_by_private_ip_id(
                    oci.core.models.GetPublicIpByPrivateIpIdDetails(private_ip_id=private_ip_id)
                )
                time.sleep(1)
            except oci.exceptions.ServiceError as e:
                if e.status == 404:
                    return
                raise

    def _wait_public_ip_assigned(self, public_ip_id: str, timeout: int = 30) -> str:
        for _ in range(timeout):
            pub = self.network.get_public_ip(public_ip_id).data
            if pub.lifecycle_state == "ASSIGNED" and pub.ip_address:
                return pub.ip_address
            time.sleep(1)
        return self.network.get_public_ip(public_ip_id).data.ip_address or "未知"

    # ---------- 创建实例（抢机） ----------
    def list_images(self, os_name: str = "Canonical Ubuntu",
                    os_version: str = "22.04", shape: str = None) -> List[Dict]:
        kwargs = {"operating_system": os_name, "operating_system_version": os_version}
        if shape:
            kwargs["shape"] = shape
        images = self.compute.list_images(self.compartment_id, **kwargs).data
        return [{"id": img.id, "name": img.display_name} for img in images]

    def launch_instance(self, *, display_name: str, ad: str, subnet_id: str,
                        image_id: str, shape: str, ocpus: float = None,
                        memory_gb: float = None, ssh_key: str = None,
                        assign_public_ip: bool = True,
                        boot_volume_gb: int = None) -> Dict:
        """发起一次创建。容量不足会抛 ServiceError，由 grab() 捕获重试。"""
        details = oci.core.models.LaunchInstanceDetails(
            compartment_id=self.compartment_id,
            availability_domain=ad,
            display_name=display_name,
            shape=shape,
            create_vnic_details=oci.core.models.CreateVnicDetails(
                subnet_id=subnet_id, assign_public_ip=assign_public_ip,
            ),
            source_details=oci.core.models.InstanceSourceViaImageDetails(
                image_id=image_id,
                boot_volume_size_in_gbs=boot_volume_gb,
            ),
        )
        # 弹性规格（A1.Flex / E*.Flex）才需要指定 ocpu/内存
        if ocpus and memory_gb:
            details.shape_config = oci.core.models.LaunchInstanceShapeConfigDetails(
                ocpus=ocpus, memory_in_gbs=memory_gb,
            )
        if ssh_key:
            details.metadata = {"ssh_authorized_keys": ssh_key}

        inst = self.compute.launch_instance(details).data
        return {"id": inst.id, "name": inst.display_name, "state": inst.lifecycle_state}

    def grab(self, *, retries: int = 100, interval: int = 60,
             ads: List[str] = None, on_attempt=None, **launch_kwargs) -> Dict:
        """
        抢机主循环：容量不足就换可用域、隔一会儿再试，直到成功或试完。
        on_attempt(i, ad, msg) 回调用于上报进度。
        """
        all_ads = ads or self.list_availability_domains()
        last_err = None
        for i in range(1, retries + 1):
            ad = all_ads[(i - 1) % len(all_ads)]  # 轮流换可用域
            try:
                if on_attempt:
                    on_attempt(i, ad, "尝试创建…")
                res = self.launch_instance(ad=ad, **launch_kwargs)
                if on_attempt:
                    on_attempt(i, ad, f"成功！实例 {res['name']}")
                return {"ok": True, "attempt": i, **res}
            except oci.exceptions.ServiceError as e:
                last_err = e
                msg = (e.message or "")[:120]
                retryable = (e.code in RETRYABLE_CODES or e.status in (429, 500))
                if on_attempt:
                    on_attempt(i, ad, f"失败({e.status} {e.code}): {msg}")
                if not retryable:
                    return {"ok": False, "error": f"{e.code}: {msg}", "fatal": True}
                time.sleep(interval)
            except Exception as e:  # noqa
                last_err = e
                if on_attempt:
                    on_attempt(i, ad, f"异常: {e}")
                time.sleep(interval)
        return {"ok": False, "error": f"试了{retries}次仍失败: {last_err}", "fatal": False}

    # ---------- 概览/配额 ----------
    def overview(self) -> Dict:
        insts = self.list_instances()
        running = sum(1 for x in insts if x["state"] == "RUNNING")
        return {
            "account": self.account.name,
            "region": self.account.region,
            "total": len(insts),
            "running": running,
            "stopped": len(insts) - running,
        }

    # ====================================================================
    #  统计概览：成本 / 流量 / 配额 / 订阅 / 租户信息
    # ====================================================================
    def tenancy_name(self) -> str:
        if self._tenancy_name is None:
            try:
                self._tenancy_name = self.identity.get_tenancy(self.account.tenancy).data.name
            except Exception as e:
                log.warning("取租户名失败: %s", e)
                self._tenancy_name = self.account.tenancy[-12:]
        return self._tenancy_name

    def home_region(self) -> str:
        """租户 home region；Usage API 必须打这个区域。"""
        if self._home_region is None:
            try:
                subs = self.identity.list_region_subscriptions(self.account.tenancy).data
                home = next((s.region_name for s in subs if s.is_home_region), None)
                self._home_region = home or self.account.region
            except Exception as e:
                log.warning("取 home region 失败: %s", e)
                self._home_region = self.account.region
        return self._home_region

    @property
    def usage(self):
        if self._usage is None:
            cfg = dict(self.config)
            cfg["region"] = self.home_region()
            self._usage = oci.usage_api.UsageapiClient(cfg)
        return self._usage

    @staticmethod
    def _month_starts(months: int) -> tuple:
        """返回 (起始月初, 结束月初) 的 UTC datetime，都对齐到月初。
        Usage API 的 MONTHLY 粒度要求起止都是某月 1 号 00:00:00。"""
        now = datetime.datetime.now(datetime.timezone.utc)
        first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        # 结束 = 下月月初（含当月）
        if first.month == 12:
            end = first.replace(year=first.year + 1, month=1)
        else:
            end = first.replace(month=first.month + 1)
        # 起始 = 往前推 months-1 个月
        y, m = first.year, first.month
        for _ in range(months - 1):
            m -= 1
            if m == 0:
                m = 12
                y -= 1
        start = first.replace(year=y, month=m)
        return start, end

    def monthly_cost(self, months: int = 3) -> List[Dict]:
        """按月成本。需要租户开通 Usage API 且 user 有读权限。"""
        start, end = self._month_starts(months)
        details = oci.usage_api.models.RequestSummarizedUsagesDetails(
            tenant_id=self.account.tenancy,
            time_usage_started=start, time_usage_ended=end,
            granularity="MONTHLY", query_type="COST",
        )
        resp = self.usage.request_summarized_usages(details)
        buckets = {}
        cur = "USD"
        for it in resp.data.items:
            month = str(it.time_usage_started)[:7]
            buckets[month] = buckets.get(month, 0) + float(it.computed_amount or 0)
            if it.currency:
                cur = it.currency
        out = [{"month": m, "amount": round(v, 2), "currency": cur}
               for m, v in buckets.items()]
        out.sort(key=lambda x: x["month"])
        return out

    def monthly_traffic(self, months: int = 3) -> List[Dict]:
        """
        按月出站流量（估算）。Usage API 没有单独「流量」口径，
        这里取 USAGE 里单位为 GB、且服务名含 transfer/network 的部分汇总。
        """
        start, end = self._month_starts(months)
        details = oci.usage_api.models.RequestSummarizedUsagesDetails(
            tenant_id=self.account.tenancy,
            time_usage_started=start, time_usage_ended=end,
            granularity="MONTHLY", query_type="USAGE", group_by=["service"],
        )
        resp = self.usage.request_summarized_usages(details)
        buckets: Dict[str, float] = {}
        fallback: Dict[str, float] = {}
        for it in resp.data.items:
            unit = (it.unit or "").upper()
            if "GB" not in unit:
                continue
            qty = float(it.computed_quantity or 0)
            month = str(it.time_usage_started)[:7]
            fallback[month] = fallback.get(month, 0) + qty
            svc = (it.service or "").lower()
            if any(k in svc for k in ("transfer", "network", "outbound")):
                buckets[month] = buckets.get(month, 0) + qty
        src = buckets or fallback
        out = [{"month": m, "gb": round(v, 2)} for m, v in src.items()]
        out.sort(key=lambda x: x["month"])
        return out

    def _limit_values(self, service: str) -> List[Dict]:
        try:
            vals = oci.pagination.list_call_get_all_results(
                self.limits.list_limit_values, self.compartment_id, service
            ).data
        except oci.exceptions.ServiceError as e:
            log.warning("取 %s 限额失败: %s", service, e)
            return []
        agg: Dict[str, float] = {}
        for v in vals:
            # 同一 limit 在多个可用域，取总和
            agg[v.name] = agg.get(v.name, 0) + (v.value or 0)
        return [{"name": k, "value": int(val)} for k, val in sorted(agg.items())]

    def quotas(self) -> Dict:
        """计算/块存储 服务限额。"""
        return {
            "compute": self._limit_values("compute"),
            "block_storage": self._limit_values("block-storage"),
        }

    def subscription_info(self) -> Dict:
        """
        订阅信息（尽力而为）。免费/试用账号常常公共接口取不到完整信息，
        取不到就返回 available=False，不伪造内部字段。
        """
        try:
            sc = oci.tenant_manager_control_plane.SubscriptionClient(self.config)
            data = sc.list_subscriptions(compartment_id=self.account.tenancy).data
            items = getattr(data, "items", data)
            if not items:
                return {"available": False, "note": "无订阅记录（多为免费试用账号）"}
            s = items[0]
            d = oci.util.to_dict(s)
            return {
                "available": True,
                "service_name": d.get("service_name") or d.get("classic_subscription_id"),
                "status": d.get("status") or d.get("lifecycle_state"),
                "start": d.get("time_start"),
                "end": d.get("time_end"),
                "raw": d,
            }
        except Exception as e:
            log.info("订阅信息不可用 (%s): %s", self.account.name, e)
            return {"available": False, "note": f"公共接口不可用: {str(e)[:80]}"}

    def account_overview(self, months: int = 3) -> Dict:
        """聚合一个账号的概览数据，单项失败不影响其他项。"""
        errs = {}

        def safe(fn, default, key=None):
            try:
                return fn()
            except Exception as e:
                log.warning("%s overview 子项失败: %s", self.account.name, e)
                if key:
                    errs[key] = str(e)[:120]
                return default

        return {
            "account": self.account.name,
            "region": self.account.region,
            "tenancy_name": safe(self.tenancy_name, self.account.tenancy[-12:]),
            "cost": safe(lambda: self.monthly_cost(months), [], "cost"),
            "traffic": safe(lambda: self.monthly_traffic(months), [], "traffic"),
            "quotas": safe(self.quotas, {"compute": [], "block_storage": []}),
            "subscription": safe(self.subscription_info, {"available": False}),
            "errors": errs,
        }

    # ====================================================================
    #  用户管理 (IAM)
    # ====================================================================
    def _group_map(self) -> Dict[str, str]:
        """group_id -> group_name"""
        groups = oci.pagination.list_call_get_all_results(
            self.identity.list_groups, self.account.tenancy
        ).data
        return {g.id: g.name for g in groups}

    def list_users(self) -> List[Dict]:
        users = oci.pagination.list_call_get_all_results(
            self.identity.list_users, self.account.tenancy
        ).data
        gmap = self._group_map()
        out = []
        for u in users:
            # 查所属组
            try:
                mems = self.identity.list_user_group_memberships(
                    self.account.tenancy, user_id=u.id).data
                groups = [gmap.get(m.group_id, m.group_id[-8:]) for m in mems]
            except Exception:
                groups = []
            out.append({
                "id": u.id,
                "name": u.name,
                "email": u.email or "",
                "state": u.lifecycle_state,
                "groups": groups,
                "mfa": bool(u.is_mfa_activated),
                "time_created": str(u.time_created),
            })
        return out

    def create_user(self, name: str, email: str = None,
                    description: str = None, group: str = "Administrators") -> Dict:
        """创建用户，并尝试加入指定用户组（默认 Administrators）。"""
        details = oci.identity.models.CreateUserDetails(
            compartment_id=self.account.tenancy,
            name=name,
            description=description or name,
            email=email,
        )
        user = self.identity.create_user(details).data
        joined = None
        if group:
            gmap = {v: k for k, v in self._group_map().items()}
            gid = gmap.get(group)
            if gid:
                self.identity.add_user_to_group(
                    oci.identity.models.AddUserToGroupDetails(
                        user_id=user.id, group_id=gid))
                joined = group
        return {"id": user.id, "name": user.name, "group": joined}

    def delete_user(self, user_id: str) -> str:
        self.identity.delete_user(user_id)
        return "用户已删除"

    def reset_password(self, user_id: str) -> str:
        """重置/生成控制台一次性密码。"""
        pw = self.identity.create_or_reset_ui_password(user_id).data
        return pw.password  # 一次性，调用方需立即展示

    def clear_user_2fa(self, user_id: str) -> int:
        """删除某用户的所有 MFA TOTP 设备，返回清除数量。"""
        devices = self.identity.list_mfa_totp_devices(user_id).data
        n = 0
        for d in devices:
            self.identity.delete_mfa_totp_device(user_id, d.id)
            n += 1
        return n

    def clear_all_2fa(self) -> Dict:
        """清除所有用户的 2FA。"""
        total = 0
        affected = 0
        for u in self.list_users():
            cnt = self.clear_user_2fa(u["id"])
            if cnt:
                affected += 1
                total += cnt
        return {"users": affected, "devices": total}
