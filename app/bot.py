"""
Telegram 机器人：内联键盘菜单。
/start → 选账号 → 选实例 → 开机/关机/重启/换IP/终止
只有 config 里 admin_ids 白名单能操作。
"""
import asyncio
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
)

from .service import Service

log = logging.getLogger("oci_manager.bot")

STATE_EMOJI = {"RUNNING": "🟢", "STOPPED": "🔴", "STARTING": "🟡",
               "STOPPING": "🟡", "PROVISIONING": "🟡", "TERMINATING": "⚫"}


class OciBot:
    def __init__(self, service: Service, token: str, admin_ids: list):
        self.service = service
        self.admin_ids = set(str(x) for x in admin_ids)
        self.app = Application.builder().token(token).build()
        self._register()

    def _register(self):
        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("jobs", self.cmd_jobs))
        self.app.add_handler(CallbackQueryHandler(self.on_callback))

    def _is_admin(self, update: Update) -> bool:
        if not self.admin_ids:  # 没配白名单 = 不限制（自担风险）
            return True
        return str(update.effective_user.id) in self.admin_ids

    # ---------- 命令 ----------
    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update):
            await update.message.reply_text("⛔ 无权限")
            return
        await update.message.reply_text(
            "🛰 *甲骨文管理*\n选择账号：",
            parse_mode="Markdown", reply_markup=self._accounts_kb())

    async def cmd_jobs(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update):
            return
        jobs = self.service.list_jobs()
        if not jobs:
            await update.message.reply_text("当前没有抢机任务")
            return
        lines = []
        for j in jobs[:10]:
            lines.append(f"`{j['id'][:8]}` {j['account']} — {j['status']} "
                         f"(试 {j['attempts']} 次)")
        await update.message.reply_text("📋 抢机任务：\n" + "\n".join(lines),
                                        parse_mode="Markdown")

    # ---------- 键盘 ----------
    def _accounts_kb(self):
        btns = [[InlineKeyboardButton(f"📦 {n}", callback_data=f"acc:{n}")]
                for n in self.service.account_names()]
        return InlineKeyboardMarkup(btns or [[InlineKeyboardButton("无账号", callback_data="noop")]])

    async def _instances_kb(self, account: str):
        insts = await asyncio.to_thread(self.service.list_instances, account)
        btns = []
        for x in insts:
            emoji = STATE_EMOJI.get(x["state"], "⚪")
            label = f"{emoji} {x['name']} | {x['public_ip'] or '无IP'}"
            btns.append([InlineKeyboardButton(label, callback_data=f"inst:{account}:{x['id']}")])
        btns.append([InlineKeyboardButton("⬅️ 返回账号", callback_data="back:acc")])
        return InlineKeyboardMarkup(btns), insts

    def _actions_kb(self, account: str, iid: str):
        a = lambda act: f"act:{act}:{account}:{iid}"
        rows = [
            [InlineKeyboardButton("▶️ 开机", callback_data=a("START")),
             InlineKeyboardButton("⏹ 关机", callback_data=a("STOP"))],
            [InlineKeyboardButton("🔄 重启", callback_data=a("SOFTRESET")),
             InlineKeyboardButton("♻️ 换IP", callback_data=a("CHIP"))],
            [InlineKeyboardButton("🗑 终止", callback_data=a("TERM"))],
            [InlineKeyboardButton("⬅️ 返回实例", callback_data=f"acc:{account}")],
        ]
        return InlineKeyboardMarkup(rows)

    # ---------- 回调路由 ----------
    async def on_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        if not self._is_admin(update):
            await q.edit_message_text("⛔ 无权限")
            return
        data = q.data

        try:
            if data == "back:acc":
                await q.edit_message_text("选择账号：", reply_markup=self._accounts_kb())

            elif data.startswith("acc:"):
                account = data.split(":", 1)[1]
                kb, insts = await self._instances_kb(account)
                await q.edit_message_text(
                    f"账号 *{account}* — {len(insts)} 台实例：",
                    parse_mode="Markdown", reply_markup=kb)

            elif data.startswith("inst:"):
                _, account, iid = data.split(":", 2)
                await q.edit_message_text(
                    f"实例操作 — `{iid[-12:]}`",
                    parse_mode="Markdown", reply_markup=self._actions_kb(account, iid))

            elif data.startswith("act:"):
                await self._do_action(q, data)

        except Exception as e:
            log.exception("回调出错")
            await q.edit_message_text(f"❌ 出错: {e}")

    async def _do_action(self, q, data: str):
        _, act, account, iid = data.split(":", 3)
        await q.edit_message_text(f"⏳ 执行 {act} …")
        try:
            if act == "CHIP":
                msg = await asyncio.to_thread(self.service.change_ip, account, iid)
            elif act == "TERM":
                msg = await asyncio.to_thread(self.service.terminate, account, iid)
            else:
                msg = await asyncio.to_thread(self.service.power_action, account, iid, act)
            await q.edit_message_text(
                f"✅ {msg}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                    "⬅️ 返回", callback_data=f"acc:{account}")]]))
        except Exception as e:
            await q.edit_message_text(
                f"❌ 失败: {e}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                    "⬅️ 返回", callback_data=f"acc:{account}")]]))

    # ---------- 生命周期（与 web 共用一个 event loop）----------
    async def start(self):
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        log.info("Telegram 机器人已启动")

    async def stop(self):
        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()
