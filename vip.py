# vip.py - 修复定时任务和导入问题

#!/usr/bin/env python3
import logging
import asyncio
import datetime as dt
import os
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    filters,
)

from config import BOT_TOKEN, ADMIN_ID
from utils import is_user_following_channel
from database import init_db
from handlers.user import (
    start,
    check_follow_callback,
    user_query_time,
    back_to_user_menu,
    restart_callback,
    contact_admin_callback,
    handle_user_message,
    reply_user_callback,
    handle_admin_reply,
    user_buy_usdt,
    usdt_plan_callback,
    check_usdt_payment_callback,
    clean_expired_orders,
)
from handlers.admin import (
    cmd_add_trial,
    cmd_add_permanent,
    cmd_extend,
    cmd_kick,
    cmd_unban,
    back_to_admin_menu,
    broadcast_confirm_callback,
    broadcast_cancel_callback,
    admin_stats,
    admin_add_trial,
    admin_add_permanent,
    admin_extend,
    admin_kick,
    admin_unban,
    admin_members,
    admin_trials,
    admin_banned,
    check_expired,
    admin_reply_callback,
    admin_reply_command,
    admin_usdt_orders_callback,
    admin_usdt_orders_history_callback,
    admin_confirm_usdt_callback,
    admin_broadcast_callback,
    handle_broadcast,
    cmd_check_user,
    cmd_add_plan,
    cmd_del_plan,
    cmd_toggle_plan,
    cmd_add_address,
    cmd_del_address,
    admin_user_manage_callback,
    admin_member_manage_callback,
    admin_plans_callback,
    admin_addresses_callback,
    # ✅ 新增系统设置
    admin_settings_callback,
    admin_set_group_callback,
    admin_set_channel_callback,
    admin_set_trial_callback,
    admin_set_remind_callback,
    admin_set_timeout_callback,
    admin_set_delete_delay_callback,
    admin_set_invite_link_callback,
    admin_set_channel_link_callback,
    admin_set_member_remind_callback,
)
from handlers.group import new_member_handler, left_member_handler
from handlers.join_request import handle_join_request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

# 🔒 安全：隐藏敏感的 HTTP 请求详情
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.Application").setLevel(logging.WARNING)

# 分布式锁标记
WORKER_ID = os.environ.get("WORKER_ID", "default")


def main():
    # 初始化数据库
    init_db()

    # 启动时验证数据库完整性
    from database import db_execute, get_user_status

    # 显示所有付费用户
    paid_users = db_execute(
        "SELECT user_id, expire_time, is_permanent FROM users WHERE expire_time IS NOT NULL OR is_permanent=1"
    ).fetchall()
    logging.info(f"=== 启动时数据库状态 ===")
    logging.info(f"付费用户总数: {len(paid_users)}")

    for user in paid_users:
        user_id = user[0]
        expire_time = user[1]
        is_permanent = user[2]
        is_valid, status = get_user_status(user_id)
        logging.info(f"用户 {user_id}: 永久={is_permanent}, 到期={expire_time}, 有效={is_valid}, 状态={status}")

    # 🔧 修复：数据库文件路径
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vip.db")
    logging.info(f"数据库文件位置: {db_path}")
    logging.info(f"数据库文件存在: {os.path.exists(db_path)}")

    app = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(True).build()

    # ================= 命令 =================
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add_trial", cmd_add_trial))
    app.add_handler(CommandHandler("add_permanent", cmd_add_permanent))
    app.add_handler(CommandHandler("extend", cmd_extend))
    app.add_handler(CommandHandler("kick", cmd_kick))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("reply", admin_reply_command))
    app.add_handler(CommandHandler("check_user", cmd_check_user))
    app.add_handler(CommandHandler("addplan", cmd_add_plan))
    app.add_handler(CommandHandler("delplan", cmd_del_plan))
    app.add_handler(CommandHandler("toggleplan", cmd_toggle_plan))
    app.add_handler(CommandHandler("addaddr", cmd_add_address))
    app.add_handler(CommandHandler("deladdr", cmd_del_address))
    app.add_handler(CallbackQueryHandler(broadcast_confirm_callback, pattern="^broadcast_confirm$"))
    app.add_handler(CallbackQueryHandler(broadcast_cancel_callback, pattern="^broadcast_cancel$"))
    

    # ================= 消息处理器 =================
    app.add_handler(MessageHandler(
        ~filters.COMMAND & filters.Chat(chat_id=ADMIN_ID) & filters.ChatType.PRIVATE,
        handle_broadcast
    ), group=1)

    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Chat(chat_id=ADMIN_ID) & filters.ChatType.PRIVATE,
        handle_admin_reply
    ), group=2)

    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE & ~filters.Chat(chat_id=ADMIN_ID),
        handle_user_message
    ), group=3)

    # ================= 群组消息事件 =================
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_member_handler))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, left_member_handler))

    # ================= 入群申请 =================
    app.add_handler(ChatJoinRequestHandler(handle_join_request))

    # ================= 回调 =================
    app.add_handler(CallbackQueryHandler(check_follow_callback, pattern="^check_follow$"))
    app.add_handler(CallbackQueryHandler(user_query_time, pattern="^user_query$"))
    app.add_handler(CallbackQueryHandler(back_to_user_menu, pattern="^back_to_user_menu$"))
    app.add_handler(CallbackQueryHandler(restart_callback, pattern="^restart$"))
    app.add_handler(CallbackQueryHandler(contact_admin_callback, pattern="^contact_admin$"))
    app.add_handler(CallbackQueryHandler(reply_user_callback, pattern="^reply_user_"))
    app.add_handler(CallbackQueryHandler(user_buy_usdt, pattern="^user_buy_usdt$"))
    app.add_handler(CallbackQueryHandler(usdt_plan_callback, pattern="^usdt_plan_"))
    app.add_handler(CallbackQueryHandler(check_usdt_payment_callback, pattern="^check_usdt_"))
    app.add_handler(CallbackQueryHandler(back_to_admin_menu, pattern="^back_to_admin_menu$"))
    app.add_handler(CallbackQueryHandler(admin_stats, pattern="^admin_stats$"))
    app.add_handler(CallbackQueryHandler(admin_add_trial, pattern="^admin_add_trial$"))
    app.add_handler(CallbackQueryHandler(admin_add_permanent, pattern="^admin_add_permanent$"))
    app.add_handler(CallbackQueryHandler(admin_extend, pattern="^admin_extend$"))
    app.add_handler(CallbackQueryHandler(admin_kick, pattern="^admin_kick$"))
    app.add_handler(CallbackQueryHandler(admin_unban, pattern="^admin_unban$"))
    app.add_handler(CallbackQueryHandler(admin_members, pattern="^admin_members$"))
    app.add_handler(CallbackQueryHandler(admin_trials, pattern="^admin_trials$"))
    app.add_handler(CallbackQueryHandler(admin_banned, pattern="^admin_banned$"))
    app.add_handler(CallbackQueryHandler(admin_reply_callback, pattern="^admin_reply$"))
    app.add_handler(CallbackQueryHandler(admin_usdt_orders_callback, pattern="^admin_usdt_orders$"))
    app.add_handler(CallbackQueryHandler(admin_usdt_orders_history_callback, pattern="^admin_usdt_orders_history"))
    app.add_handler(CallbackQueryHandler(admin_confirm_usdt_callback, pattern="^admin_confirm_usdt_"))
    app.add_handler(CallbackQueryHandler(admin_broadcast_callback, pattern="^admin_broadcast$"))
    app.add_handler(CallbackQueryHandler(admin_user_manage_callback, pattern="^admin_user_manage$"))
    app.add_handler(CallbackQueryHandler(admin_member_manage_callback, pattern="^admin_member_manage$"))
    app.add_handler(CallbackQueryHandler(admin_plans_callback, pattern="^admin_plans$"))
    app.add_handler(CallbackQueryHandler(admin_addresses_callback, pattern="^admin_addresses$"))
    # 系统设置
    app.add_handler(CallbackQueryHandler(admin_settings_callback, pattern="^admin_settings$"))
    app.add_handler(CallbackQueryHandler(admin_set_group_callback, pattern="^admin_set_group$"))
    app.add_handler(CallbackQueryHandler(admin_set_channel_callback, pattern="^admin_set_channel$"))
    app.add_handler(CallbackQueryHandler(admin_set_trial_callback, pattern="^admin_set_trial$"))
    app.add_handler(CallbackQueryHandler(admin_set_remind_callback, pattern="^admin_set_remind$"))
    app.add_handler(CallbackQueryHandler(admin_set_timeout_callback, pattern="^admin_set_timeout$"))
    app.add_handler(CallbackQueryHandler(admin_set_delete_delay_callback, pattern="^admin_set_delete$"))
    app.add_handler(CallbackQueryHandler(admin_set_invite_link_callback, pattern="^admin_set_invite_link$"))
    app.add_handler(CallbackQueryHandler(admin_set_channel_link_callback, pattern="^admin_set_channel_link$"))
    app.add_handler(CallbackQueryHandler(admin_set_member_remind_callback, pattern="^admin_set_member_remind$"))

    # ================= 定时任务 =================
    if app.job_queue:
        enable_scheduler = os.environ.get("ENABLE_SCHEDULER", "true").lower() == "true"

        # 初始化分布式锁表
        from scheduler_lock import init_scheduler_locks_table
        init_scheduler_locks_table()

        if enable_scheduler:
            # ✅ 使用带锁的检查任务
            async def check_expired_with_lock(context):
                logging.info(f"定时任务触发: check_expired_with_lock (Worker: {WORKER_ID})")
                from scheduler_lock import SchedulerLock
                lock = SchedulerLock("check_expired", timeout=120)
                if not lock.acquire():
                    logging.info(f"Worker {WORKER_ID}: 锁已被占用，跳过检查")
                    return
                try:
                    await check_expired(context)
                finally:
                    lock.release()
                logging.info(f"定时任务完成: check_expired_with_lock (Worker: {WORKER_ID})")

            async def check_all_group_members_with_lock(context):
                from scheduler_lock import SchedulerLock
                lock = SchedulerLock("check_all_members", timeout=600)
                if not lock.acquire():
                    logging.info(f"Worker {WORKER_ID}: 全量检查锁已被占用，跳过")
                    return
                try:
                    await _check_all_group_members(context)
                finally:
                    lock.release()

            async def clean_database_with_lock(context):
                from scheduler_lock import SchedulerLock
                lock = SchedulerLock("clean_database", timeout=300)
                if not lock.acquire():
                    logging.info(f"Worker {WORKER_ID}: 清理锁已被占用，跳过")
                    return
                try:
                    await _clean_database_job(context)
                finally:
                    lock.release()

            app.job_queue.run_repeating(check_expired_with_lock, interval=300, first=10)
            app.job_queue.run_repeating(check_all_group_members_with_lock, interval=1800, first=60)
            app.job_queue.run_repeating(clean_database_with_lock, interval=300, first=15)
            app.job_queue.run_daily(clean_database_with_lock, time=dt.time(hour=3, minute=0), days=tuple(range(7)))

    # 启动时恢复待处理订单
    from handlers.user import restore_orders_on_startup
    restore_orders_on_startup()

    logging.info(f"机器人启动 (Worker: {WORKER_ID})，使用 polling 模式")
    app.run_polling()


# ================= 提取的定时任务辅助函数 =================
async def _check_all_group_members(context):
    """检查数据库中所有用户的频道关注状态，并记录群内用户"""
    import config
    from database import is_admin as db_is_admin, db_execute, get_user, add_trial
    from utils import is_user_following_channel

    logging.info("开始全量检查群成员...")
    try:
        # 🔧 无法直接获取所有成员，改为从数据库获取所有未封禁用户
        rows = db_execute("SELECT user_id FROM users WHERE is_banned=0").fetchall()

        checked = 0
        kicked = 0
        new_in_group = 0

        for (user_id,) in rows:
            if user_id == context.bot.id or db_is_admin(user_id):
                continue

            # 检查用户是否在群组中
            try:
                member = await context.bot.get_chat_member(config.GROUP_ID, user_id)

                # 不在群组中，跳过
                if member.status not in ["member", "administrator", "creator"]:
                    continue

                # 跳过管理员
                if member.status in ["administrator", "creator"]:
                    continue
            except Exception as e:
                # 无法获取状态，可能不在群组
                continue

            checked += 1

            # 检查频道关注
            is_following = await is_user_following_channel(context, user_id)

            if is_following is None:
                logging.warning(f"用户 {user_id} 频道关注检查失败（API异常），跳过处理")
            elif not is_following:
                db_execute("UPDATE users SET needs_channel_check=1 WHERE user_id=?", (user_id,))
                kicked += 1
                logging.info(f"用户 {user_id} 未关注频道，已标记需要检查")
                try:
                    await context.bot.send_message(
                        user_id,
                        f"⚠️ 检测到您未关注频道，请关注后继续使用服务。\n\n"
                        f"👉 {config.CHANNEL_LINK}"
                    )
                except:
                    pass

            await asyncio.sleep(0.05)

        logging.info(f"全量检查完成：已检查 {checked} 人，移除 {kicked} 人")

    except Exception as e:
        logging.error(f"全量检查失败: {e}")

async def _clean_database_job(context):
    """定时清理数据库任务"""
    from database import db_execute, mark_address_idle, update_expired_pending_orders, clean_old_orders
    logging.info(f"Worker {WORKER_ID}: 开始执行数据库清理任务...")
    updated = update_expired_pending_orders()
    logging.info(f"已将 {updated} 个超时订单转为过期并释放地址")
    deleted = clean_old_orders()
    logging.info(f"数据库清理完成，共删除 {deleted} 条记录")


if __name__ == "__main__":
    main()
