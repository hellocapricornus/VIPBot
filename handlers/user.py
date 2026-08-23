# user.py - 完整修复版本（使用地址池查询交易）

import asyncio
import logging
import random
import time
import hashlib
import json
import requests
import secrets
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import config
from config import ADMIN_ID
from database import (
    now, BEIJING, get_user, get_user_status, is_admin, is_user_following_channel,
    has_valid_membership, save_message, extend_member, unban_user, db_execute,
    add_trial, get_pending_orders, mark_address_idle, mark_address_used,
    get_available_address
)
from utils import send_temp

# ================= 全局变量 =================
pending_usdt_orders = {}


# ================= 恢复订单 =================
def restore_orders_on_startup():
    """机器人启动时恢复未完成的订单"""
    global pending_usdt_orders
    rows = get_pending_orders()
    for row in rows:
        order_id, user_id, plan_name, days, amount, created_at = row[:6]
        address = row[6] if len(row) > 6 else ""
        amount_key = f"{amount:.2f}"
        if isinstance(created_at, str):
            try:
                created_dt = datetime.fromisoformat(created_at).astimezone(BEIJING)
                created_ts = created_dt.timestamp()
            except:
                created_ts = time.time()
        else:
            created_ts = created_at.timestamp() if hasattr(created_at, 'timestamp') else time.time()
        pending_usdt_orders[amount_key] = {
            "order_id": order_id,
            "user_id": user_id,
            "days": days,
            "amount": amount,
            "plan_name": plan_name,
            "created_at": created_ts,
            "address": address
        }
        logging.info(f"恢复订单: {amount_key} - 用户 {user_id}")
    logging.info(f"共恢复 {len(pending_usdt_orders)} 个待处理订单")


# ================= 生成唯一金额 =================
def generate_unique_amount(base_price: float, user_id: int) -> float:
    """生成唯一金额，确保金额键不与现有待支付订单冲突。

    通过检查 pending_usdt_orders 避免两个用户生成相同金额导致订单覆盖/支付错配。
    """
    for _ in range(100):
        random_cents = secrets.randbelow(99) + 1
        user_factor = user_id % 100
        unique_cents = ((random_cents + user_factor) % 99) + 1
        amount = base_price + unique_cents / 100
        if f"{amount:.2f}" not in pending_usdt_orders:
            return amount
    # 极端情况：扩大随机范围到 0.0001 精度
    return base_price + (secrets.randbelow(9900) + 1) / 10000


# ================= 交易查询（使用地址池）=================
async def check_usdt_transaction_with_retry(amount: float, max_retries: int = 3, retry_delay: float = 3) -> dict:
    """带重试的 USDT 交易查询"""
    last_error = None
    for retry in range(max_retries):
        try:
            result = await check_usdt_transaction(amount, retry)
            if result["success"]:
                return result
            if retry < max_retries - 1:
                await asyncio.sleep(retry_delay * (retry + 1))
        except Exception as e:
            last_error = str(e)
            logging.warning(f"查询交易失败 (重试 {retry+1}/{max_retries}): {e}")
            if retry < max_retries - 1:
                await asyncio.sleep(retry_delay * (retry + 1))
    return {"success": False, "message": last_error or "查询失败"}


async def check_usdt_transaction(amount: float, retry_count: int = 0) -> dict:
    """通过 TronGrid API 查询所有地址池中的 USDT 交易"""
    import concurrent.futures

    addresses = db_execute("SELECT address FROM vip_addresses").fetchall()
    if not addresses:
        return {"success": False, "message": "无可用收款地址"}

    # ✅ 把同步请求放到线程池，不阻塞事件循环
    loop = asyncio.get_event_loop()

    # 定义同步查询函数
    def query_single_address(address):
        url = f"https://api.trongrid.io/v1/accounts/{address}/transactions/trc20"
        params = {"limit": 50, "only_confirmed": True}
        try:
            response = requests.get(url, params=params, timeout=15)
            if response.status_code == 200:
                data = response.json()
                transactions = data.get("data", [])
                for tx in transactions:
                    if tx.get("type") == "Transfer" and tx.get("token_info", {}).get("symbol") == "USDT":
                        tx_amount = float(tx.get("value", 0)) / 1000000
                        to_address = tx.get("to")
                        if abs(tx_amount - amount) < 0.01 and to_address == address:
                            tx_id = tx.get("transaction_id")
                            if not is_transaction_processed(tx_id):
                                return {"success": True, "tx_id": tx_id, "amount": tx_amount, "address": address}
        except:
            pass
        return None

    try:
        # 并发查询所有地址
        tasks = [loop.run_in_executor(None, query_single_address, address) for (address,) in addresses]

        for task in asyncio.as_completed(tasks):
            result = await task
            if result:
                return result

        if retry_count < 2:
            return {"success": False, "message": "未找到匹配交易", "retry": True}
        return {"success": False, "message": "未找到匹配交易"}

    except Exception as e:
        logging.error(f"查询 USDT 交易失败: {e}", exc_info=True)
        return {"success": False, "message": str(e)}


def is_transaction_processed(tx_id: str) -> bool:
    """检查交易是否已经处理过"""
    row = db_execute("SELECT 1 FROM processed_transactions WHERE tx_id=?", (tx_id,)).fetchone()
    return row is not None


def mark_transaction_processed(tx_id: str, user_id: int, days: int):
    """标记交易已处理"""
    db_execute("""
        INSERT INTO processed_transactions (tx_id, user_id, days, processed_at)
        VALUES (?, ?, ?, ?)
    """, (tx_id, user_id, days, now().isoformat()))


def clean_expired_orders():
    """清理内存中过期的订单"""
    current_time = time.time()
    expired_keys = []
    for amount_key, order in list(pending_usdt_orders.items()):
        if current_time - order["created_at"] > config.USDT_ORDER_TIMEOUT:
            expired_keys.append(amount_key)
            db_execute("""
                UPDATE usdt_orders SET status='expired' 
                WHERE order_id=? AND status='pending'
            """, (order["order_id"],))
            # ✅ 释放地址
            if "address" in order:
                mark_address_idle(order["address"])
                logging.info(f"订单 {order['order_id']} 过期，释放地址 {order['address']}")
    for key in expired_keys:
        del pending_usdt_orders[key]
        logging.info(f"清理过期订单: {key}")


# ================= 频道检查函数 =================
async def check_and_handle_channel(context, user_id, kick_only=False):
    """检查频道关注并处理，返回是否关注"""
    is_following = await is_user_following_channel(context, user_id)
    
    if is_following is None:
        logging.warning(f"用户 {user_id} 频道关注检查失败（API异常），跳过处理")
        return None
    
    if not is_following:
        db_execute("UPDATE users SET needs_channel_check=1 WHERE user_id=?", (user_id,))
        from utils import kick_user
        await kick_user(context, user_id, "未关注频道", ban=True)
        try:
            await context.bot.send_message(
                user_id,
                f"❌ 您未关注我们的频道，无法继续使用服务。\n\n"
                f"请关注频道后重新申请加入群组。\n\n👉 {config.CHANNEL_LINK}"
            )
        except:
            pass
        return False
    db_execute("UPDATE users SET needs_channel_check=0 WHERE user_id=?", (user_id,))
    return True


# ================= 统一试用添加函数 =================
async def ensure_trial_for_user(user_id: int, context=None) -> tuple:
    row = get_user(user_id)
    is_valid, status = get_user_status(user_id)

    if row and (row[0] or row[1] == 1):
        return True, status

    if row and row[3] == 1:
        return False, status

    # ✅ 曾经有过试用记录，不再给试用
    if row and row[2]:
        return False, status

    if not row:
        add_trial(user_id)
        is_valid, status = get_user_status(user_id)
        return True, f"🧪 您已获得{int(config.TRIAL_HOURS)}小时免费试用资格！"

    return is_valid, status


# ================= 用户私聊命令 =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """用户私聊 /start"""
    if update.effective_chat.type != "private":
        return
    user_id = update.effective_user.id
    if is_admin(user_id):
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 数据统计", callback_data="admin_stats")],
            [InlineKeyboardButton("👥 用户管理", callback_data="admin_user_manage"),
             InlineKeyboardButton("💳 会员管理", callback_data="admin_member_manage")],
            [InlineKeyboardButton("💎 USDT订单", callback_data="admin_usdt_orders"),
             InlineKeyboardButton("📜 订单历史", callback_data="admin_usdt_orders_history")],
            [InlineKeyboardButton("📦 套餐管理", callback_data="admin_plans"),
             InlineKeyboardButton("🏦 地址管理", callback_data="admin_addresses")],
            [InlineKeyboardButton("📢 广播消息", callback_data="admin_broadcast"),
             InlineKeyboardButton("💬 回复用户", callback_data="admin_reply")],
            [InlineKeyboardButton("⚙️ 系统设置", callback_data="admin_settings")],
        ])
        await update.message.reply_text("👑 管理员菜单", reply_markup=keyboard)
        return
    await show_channel_guide(update, context, user_id)


async def show_channel_guide(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """显示频道关注引导"""
    is_following = await is_user_following_channel(context, user_id)
    if is_following is None:
        await update.message.reply_text("⚠️ 当前无法检查频道关注状态，请稍后重试。")
        return
    if not is_following:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 关注频道", url=config.CHANNEL_LINK)],
            [InlineKeyboardButton("✅ 我已关注", callback_data="check_follow")]
        ])
        await update.message.reply_text(
            "❌ 您需要先关注我们的频道\n\n"
            "请先关注频道，然后点击「我已关注」按钮。\n\n"
            f"👉 {config.CHANNEL_LINK}",
            reply_markup=keyboard
        )
        return
    is_valid, status = await ensure_trial_for_user(user_id, context)
    keyboard = [
        [InlineKeyboardButton("🔗 加入群组", url=config.GROUP_LINK)],
        [InlineKeyboardButton("🕒 查询会员时间", callback_data="user_query")],
        [InlineKeyboardButton("💰 购买会员", callback_data="user_buy_usdt")],
        [InlineKeyboardButton("📞 联系管理员", callback_data="contact_admin")]
    ]
    await update.message.reply_text(
        f"{status}\n\n🎛 功能菜单",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ================= 联系管理员 =================
async def contact_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """用户联系管理员"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    try:
        user = await context.bot.get_chat(user_id)
        user_name = user.full_name or user.username or str(user_id)
    except:
        user_name = str(user_id)
    await query.edit_message_text(
        "📝 请输入您要发送给管理员的消息：\n\n"
        "（直接回复此消息即可，我会帮您转达）\n\n"
        "💡 提示：请一次性发送完整消息，管理员回复后您会收到通知。",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ 返回", callback_data="back_to_user_menu")]
        ])
    )
    context.user_data['waiting_for_admin_msg'] = True
    context.user_data['user_name'] = user_name


async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理用户发送给管理员的消息"""
    if update.effective_chat.type != "private":
        return
    user_id = update.effective_user.id
    message_text = update.message.text
    if context.user_data.get('waiting_for_admin_msg'):
        user_name = context.user_data.get('user_name', str(user_id))
        save_message(user_id, ADMIN_ID, message_text)
        try:
            reply_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("💬 回复用户", callback_data=f"reply_user_{user_id}")]
            ])
            await context.bot.send_message(
                ADMIN_ID,
                f"📨 来自用户 {user_name} (ID: {user_id}) 的消息：\n\n{message_text}\n\n"
                f"💡 点击下方按钮回复该用户：",
                reply_markup=reply_keyboard
            )
            await update.message.reply_text("✅ 消息已发送给管理员，请耐心等待回复。")
        except Exception as e:
            await update.message.reply_text(f"❌ 发送失败：{e}")
        context.user_data['waiting_for_admin_msg'] = False
        context.user_data['user_name'] = None
        return
    await update.message.reply_text(
        "请先点击「联系管理员」按钮后再发送消息。\n\n"
        "1. 发送 /start\n2. 点击「联系管理员」\n3. 然后输入您的消息"
    )


async def reply_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员回复用户的回调"""
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ 只有管理员可以使用此功能。")
        return
    try:
        user_id = int(query.data.split("_")[2])
    except:
        await query.edit_message_text("❌ 无法识别用户。")
        return
    context.user_data['replying_to_user'] = user_id
    try:
        user = await context.bot.get_chat(user_id)
        user_name = user.full_name or user.username or str(user_id)
    except:
        user_name = str(user_id)
    await query.edit_message_text(
        f"📝 正在回复用户：{user_name} (ID: {user_id})\n\n"
        "请输入您要回复的内容：\n\n（直接回复此消息即可）\n\n"
        "💡 提示：回复内容将直接发送给用户。",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ 取消", callback_data="back_to_admin_menu")]
        ])
    )


async def handle_admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理管理员回复用户的消息"""
    if update.effective_chat.type != "private":
        return
    admin_id = update.effective_user.id
    if not is_admin(admin_id):
        return

    # ✅ 处理系统设置输入
    waiting_for = context.user_data.get('waiting_for')
    if waiting_for and waiting_for.startswith('set_'):
        await handle_settings_input(update, context)
        return
        
    if context.user_data.get('broadcast_mode'):
        return
    replying_to = context.user_data.get('replying_to_user')
    if replying_to:
        user_id = replying_to
        reply_text = update.message.text
        save_message(ADMIN_ID, user_id, reply_text)
        try:
            await context.bot.send_message(
                user_id,
                f"📨 管理员回复：\n\n{reply_text}\n\n"
                f"如需继续联系，请使用 /start 后点击「联系管理员」。"
            )
            await update.message.reply_text(f"✅ 已回复用户 {user_id}")
            context.user_data['replying_to_user'] = None
        except Exception as e:
            await update.message.reply_text(f"❌ 发送失败：{e}")
        return
    await update.message.reply_text(
        "请先点击「回复用户」按钮后再发送回复。\n\n"
        "1. 点击用户消息下方的「回复用户」按钮\n2. 然后输入回复内容\n\n"
        "或者使用命令：/reply 用户ID 回复内容"
    )

# ================= 系统设置输入处理 =================
async def handle_settings_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理管理员输入的系统设置"""
    waiting_for = context.user_data.get('waiting_for')
    text = update.message.text.strip()

    if text == '/cancel':
        context.user_data['waiting_for'] = None
        await update.message.reply_text("✅ 已取消")
        return

    try:
        from database import db_execute

        settings_map = {
            'set_group_id': ('GROUP_ID', '群组ID', str),
            'set_invite_link': ('GROUP_LINK', '群组邀请链接', str),
            'set_channel_id': ('CHANNEL_ID', '频道ID', str),
            'set_channel_link': ('CHANNEL_LINK', '频道链接', str),
            'set_trial_hours': ('TRIAL_HOURS', '试用时长', float),
            'set_remind_hours': ('REMIND_HOURS', '试用提醒时间', float),
            'set_member_remind': ('MEMBER_REMIND_DAYS', '会员提醒天数', float),
            'set_order_timeout': ('USDT_ORDER_TIMEOUT', '订单超时', int),
            'set_delete_delay': ('DELETE_DELAY', '删除延迟', int),
        }

        if waiting_for in settings_map:
            key, name, val_type = settings_map[waiting_for]
            value = val_type(text)

            db_execute("""
                INSERT INTO settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """, (key, str(value)))

            from config import refresh_config
            refresh_config()

            context.user_data['waiting_for'] = None
            await update.message.reply_text(f"✅ {name}已更新为：{value}\n配置已实时生效！")
        else:
            await update.message.reply_text("❌ 未知设置项")

    except ValueError:
        await update.message.reply_text("❌ 输入格式错误，请重新输入\n回复 /cancel 取消")
        
# ================= 用户回调 =================
async def check_follow_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """检查用户是否真的关注了频道"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    is_following = await is_user_following_channel(context, user_id)
    if is_following is None:
        await query.edit_message_text("⚠️ 当前无法检查频道关注状态，请稍后重试。")
        return
    if not is_following:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 关注频道", url=config.CHANNEL_LINK)],
            [InlineKeyboardButton("🔄 我已关注", callback_data="check_follow")]
        ])
        await query.edit_message_text(
            "❌ 检测到您还没有关注频道！\n\n"
            "请先点击下方按钮关注频道，关注后再点击「我已关注」。\n\n"
            "👉 关注后请等待几秒钟再点击确认。",
            reply_markup=keyboard
        )
        return
    is_valid, status = await ensure_trial_for_user(user_id, context)
    keyboard = [
        [InlineKeyboardButton("🔗 加入群组", url=config.GROUP_LINK)],
        [InlineKeyboardButton("🕒 查询会员时间", callback_data="user_query")],
        [InlineKeyboardButton("💰 购买会员", callback_data="user_buy_usdt")],
        [InlineKeyboardButton("📞 联系管理员", callback_data="contact_admin")]
    ]
    await query.edit_message_text(
        f"{status}\n\n🎛 功能菜单",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def user_query_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查询会员剩余时间"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    is_following = await is_user_following_channel(context, user_id)
    if is_following is None:
        await query.edit_message_text("⚠️ 当前无法检查频道关注状态，请稍后重试。")
        return
    if not is_following:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 关注频道", url=config.CHANNEL_LINK)],
            [InlineKeyboardButton("🔄 重新检查", callback_data="check_follow")]
        ])
        await query.edit_message_text(
            "❌ 检测到您没有关注频道！\n\n请先关注频道后重新查询。",
            reply_markup=keyboard
        )
        return
    is_valid, status = get_user_status(user_id)
    if is_valid:
        row = get_user(user_id)
        if row and row[1] == 1:
            text = "🎉 您拥有永久会员，无时间限制。"
        elif row and row[0]:
            expire = datetime.fromisoformat(row[0]).astimezone(BEIJING)
            if expire > now():
                remain = expire - now()
                days = remain.days
                hours = remain.seconds // 3600
                minutes = (remain.seconds % 3600) // 60
                text = f"⏰ 会员剩余时间: {days}天 {hours}小时 {minutes}分钟"
            else:
                text = "⚠️ 您的会员已过期，请购买续期。"
        elif row and row[2]:
            trial_start = datetime.fromisoformat(row[2]).astimezone(BEIJING)
            trial_end = trial_start + timedelta(hours=config.TRIAL_HOURS)
            if trial_end > now():
                remain = trial_end - now()
                # 使用 total_seconds 避免丢失天数
                total_hours = int(remain.total_seconds() // 3600)
                minutes = (remain.seconds % 3600) // 60
                text = f"🧪 试用剩余时间: {total_hours}小时 {minutes}分钟"
            else:
                text = "❌ 试用已结束，请购买会员。"
        else:
            text = status
    else:
        if "试用已结束" in status:
            text = "⏰ 您的试用期已结束，请购买会员后继续使用。"
        elif "会员已过期" in status:
            text = "⚠️ 您的会员已过期，请续费后继续使用。"
        else:
            text = f"❌ {status}"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️ 返回菜单", callback_data="back_to_user_menu")]
    ])
    await query.edit_message_text(text, reply_markup=keyboard)


async def back_to_user_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """返回用户菜单"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    # 取消该用户的待支付订单并释放地址
    for amount_str, order in list(pending_usdt_orders.items()):
        if order["user_id"] == user_id:
            db_execute("""
                UPDATE usdt_orders SET status='cancelled' 
                WHERE order_id=? AND status='pending'
            """, (order["order_id"],))
            if "address" in order:
                mark_address_idle(order["address"])
            del pending_usdt_orders[amount_str]
    is_following = await is_user_following_channel(context, user_id)
    if is_following is None:
        await query.edit_message_text("⚠️ 当前无法检查频道关注状态，请稍后重试。")
        return
    if not is_following:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 关注频道", url=config.CHANNEL_LINK)],
            [InlineKeyboardButton("✅ 我已关注", callback_data="check_follow")]
        ])
        await query.edit_message_text(
            "❌ 您需要先关注我们的频道\n\n"
            "请先关注频道，然后点击「我已关注」按钮。\n\n"
            f"👉 {config.CHANNEL_LINK}",
            reply_markup=keyboard
        )
        return
    is_valid, status = await ensure_trial_for_user(user_id, context)
    keyboard = [
        [InlineKeyboardButton("🔗 加入群组", url=config.GROUP_LINK)],
        [InlineKeyboardButton("🕒 查询会员时间", callback_data="user_query")],
        [InlineKeyboardButton("💰 购买会员", callback_data="user_buy_usdt")],
        [InlineKeyboardButton("📞 联系管理员", callback_data="contact_admin")]
    ]
    await query.edit_message_text(
        f"{status}\n\n🎛 功能菜单",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def restart_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """重新开始"""
    query = update.callback_query
    await query.answer()
    await start(update, context)


# ================= USDT 支付 =================
async def user_buy_usdt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """用户购买会员入口"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    is_following = await is_user_following_channel(context, user_id)
    if is_following is None:
        await query.edit_message_text("⚠️ 当前无法检查频道关注状态，请稍后重试。")
        return
    if not is_following:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 关注频道", url=config.CHANNEL_LINK)],
            [InlineKeyboardButton("🔄 重新检查", callback_data="check_follow")]
        ])
        await query.edit_message_text(
            "❌ 检测到您没有关注频道！\n\n请先关注频道后再购买会员。",
            reply_markup=keyboard
        )
        return
    from database import get_active_plans, get_available_address
    address = get_available_address()
    if not address:
        await query.edit_message_text(
            "😔 支付通道暂未开放，请联系管理员添加收款地址。",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ 返回", callback_data="back_to_user_menu")]
            ])
        )
        return
    plans = get_active_plans()
    if not plans:
        await query.edit_message_text(
            "📭 暂无可用的会员套餐，请联系管理员添加。",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ 返回", callback_data="back_to_user_menu")]
            ])
        )
        return
    keyboard = []
    for plan in plans:
        keyboard.append([
            InlineKeyboardButton(
                f"{plan['name']} - {plan['price']} USDT",
                callback_data=f"usdt_plan_{plan['plan_id']}"
            )
        ])
    keyboard.append([InlineKeyboardButton("◀️ 返回", callback_data="back_to_user_menu")])
    await query.edit_message_text(
        "💎 **购买会员（USDT 支付）**\n\n请选择套餐：",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


async def usdt_plan_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """用户选择套餐，生成唯一金额订单"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    plan_id = query.data.replace("usdt_plan_", "")
    from database import get_active_plans, get_available_address, mark_address_used
    plans = get_active_plans()
    plan = next((p for p in plans if p['plan_id'] == plan_id), None)
    if not plan:
        await query.answer("套餐不存在", show_alert=True)
        return
    name = plan['name']
    days = plan['days']
    base_price = plan['price']
    # 清理该用户的旧订单
    for amount_str, order in list(pending_usdt_orders.items()):
        if order["user_id"] == user_id:
            old_order = pending_usdt_orders[amount_str]
            db_execute("""
                UPDATE usdt_orders SET status='cancelled' 
                WHERE order_id=? AND status='pending'
            """, (old_order["order_id"],))
            if "address" in old_order:
                mark_address_idle(old_order["address"])
            del pending_usdt_orders[amount_str]
    address = get_available_address()
    if not address:
        await query.edit_message_text("😔 支付通道繁忙，请稍后重试。")
        return
    mark_address_used(address)
    unique_amount = generate_unique_amount(base_price, user_id)
    order_id = f"{user_id}_{int(time.time())}_{random.randint(1000, 9999)}"
    amount_key = f"{unique_amount:.2f}"
    pending_usdt_orders[amount_key] = {
        "order_id": order_id,
        "user_id": user_id,
        "days": days,
        "amount": unique_amount,
        "plan_name": name,
        "plan_id": plan_id,
        "status": "pending",
        "created_at": time.time(),
        "address": address
    }
    db_execute("""
        INSERT INTO usdt_orders (order_id, user_id, plan_name, days, amount, status, created_at, address)
        VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
    """, (order_id, user_id, name, days, unique_amount, now().isoformat(), address))
    clean_expired_orders()
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ 我已支付", callback_data=f"check_usdt_{amount_key}")],
        [InlineKeyboardButton("❌ 取消订单", callback_data="back_to_user_menu")]
    ])
    await query.edit_message_text(
        f"💎 **{name}** - {unique_amount:.2f} USDT\n\n"
        f"📌 **转账信息**\n"
        f"网络：**TRC20**\n"
        f"地址：`{address}`\n"
        f"金额：**{unique_amount:.2f} USDT**\n\n"
        f"📝 **订单号**：`{order_id}`\n\n"
        f"⚠️ **重要提示**\n"
        f"1. 请转账**精确金额** `{unique_amount:.2f} USDT`\n"
        f"2. 必须使用 **TRC20** 网络\n"
        f"3. 转账后点击「我已支付」\n"
        f"4. ⏰ **请在 10 分钟内完成支付**\n\n"
        f"💡 转账完成后，系统会自动检测并开通会员。",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


async def check_usdt_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """用户点击「我已支付」，检查订单状态"""
    query = update.callback_query
    await query.answer()

    amount_key = query.data.replace("check_usdt_", "")
    clean_expired_orders()

    if amount_key not in pending_usdt_orders:
        await query.edit_message_text(
            "❌ 订单不存在或已过期，请重新购买。",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 重新购买", callback_data="user_buy_usdt")]
            ])
        )
        return

    order = pending_usdt_orders[amount_key]
    user_id = order["user_id"]

    if user_id != query.from_user.id:
        await query.answer("这不是您的订单！", show_alert=True)
        return

    if time.time() - order["created_at"] > config.USDT_ORDER_TIMEOUT:
        # 与 clean_expired_orders 行为一致：更新DB状态并释放地址
        db_execute("""
            UPDATE usdt_orders SET status='expired'
            WHERE order_id=? AND status='pending'
        """, (order["order_id"],))
        if "address" in order:
            mark_address_idle(order["address"])
        del pending_usdt_orders[amount_key]
        await query.edit_message_text("⏰ 订单已超时，请重新购买。")
        return

    await query.edit_message_text(
        f"⏳ 正在检测转账...\n\n金额：{order['amount']:.2f} USDT\n请稍等..."
    )

    result = await check_usdt_transaction_with_retry(order["amount"], max_retries=3)

    if result and result["success"]:
        unban_user(user_id)
        is_following = await is_user_following_channel(context, user_id)
        if is_following is None:
            logging.warning(f"用户 {user_id} 支付成功，但频道关注检查失败（API异常），继续处理")
        elif not is_following:
            await query.edit_message_text(
                f"⚠️ 检测到您未关注频道！\n\n👉 {config.CHANNEL_LINK}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📢 关注频道", url=config.CHANNEL_LINK)],
                    [InlineKeyboardButton("✅ 我已关注并支付", callback_data=f"check_usdt_{amount_key}")]
                ])
            )
            return

        new_expire = extend_member(user_id, order["days"])

        db_execute("""
            UPDATE usdt_orders SET status='paid', paid_at=?, tx_id=?
            WHERE order_id=?
        """, (now().isoformat(), result["tx_id"], order["order_id"]))
        mark_transaction_processed(result["tx_id"], user_id, order["days"])
        if "address" in order:
            mark_address_idle(order["address"])
        del pending_usdt_orders[amount_key]

        await query.edit_message_text(
            f"✅ **支付成功！**\n\n"
            f"套餐：{order['plan_name']}\n"
            f"金额：{order['amount']:.2f} USDT\n"
            f"会员到期时间：{new_expire.strftime('%Y-%m-%d %H:%M')}\n\n感谢您的支持！",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔗 加入群组", url=config.GROUP_LINK)],
                [InlineKeyboardButton("📊 查看状态", callback_data="user_query")],
                [InlineKeyboardButton("◀️ 返回菜单", callback_data="back_to_user_menu")]
            ]),
            parse_mode="Markdown"
        )
    else:
        remaining_time = int(config.USDT_ORDER_TIMEOUT - (time.time() - order["created_at"]))
        minutes = remaining_time // 60
        seconds = remaining_time % 60
        await query.edit_message_text(
            f"⏳ 未检测到 {order['amount']:.2f} USDT 的转账记录\n\n"
            f"请确认：\n"
            f"1. 已转账**精确金额** {order['amount']:.2f} USDT\n"
            f"2. 使用的是 **TRC20** 网络\n"
            f"3. 转账已完成（需要约 1-3 分钟确认）\n\n"
            f"⏰ 订单剩余时间：{minutes}分{seconds}秒\n\n"
            f"确认无误后请稍后再次点击「我已支付」。",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 重新检测", callback_data=f"check_usdt_{amount_key}")],
                [InlineKeyboardButton("📞 联系管理员", callback_data="contact_admin")],
                [InlineKeyboardButton("❌ 取消订单", callback_data="back_to_user_menu")]
            ])
        )
